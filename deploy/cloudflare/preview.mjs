#!/usr/bin/env node
// Open the recall app on this machine without Cloudflare or a sign-in: serves ./app the way the
// Worker's static assets do (one page for every address, the headers from _headers) and answers
// the API from made-up sample data kept in memory. For working on the screens.
//
//   node preview.mjs            then open http://127.0.0.1:8793
//   node preview.mjs --api http://127.0.0.1:8791 --token <token>   use a real recall server instead
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(fileURLToPath(new URL(".", import.meta.url)), "app");
const arg = (name) => { const at = process.argv.indexOf(name); return at > 0 ? process.argv[at + 1] : undefined; };
const PORT = Number(arg("--port") ?? process.env.PORT ?? 8793);
const REAL = arg("--api");
const TOKEN = arg("--token") ?? "preview";
const TYPES = {
	".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
	".webmanifest": "application/manifest+json", ".svg": "image/svg+xml", ".png": "image/png", ".woff2": "font/woff2",
};

// The headers under "/*" in _headers, as Cloudflare would add them.
const headers = {};
for (const line of (await readFile(join(ROOT, "_headers"), "utf8")).split("\n")) {
	if (/^\S/.test(line)) { if (Object.keys(headers).length) break; continue; }
	const [, name, value] = /^\s+([\w-]+):\s*(.+)$/.exec(line) ?? [];
	if (name) headers[name] = value;
}

// ----- sample data -----------------------------------------------------------
const ago = (minutes) => new Date(Date.now() - minutes * 60_000).toISOString().replace("Z", "");
let serial = 0;
const id = () => `memory:${(++serial).toString(16).padStart(12, "0")}`;
const memories = [
	{ id: id(), text: "The reading lamp in the study uses a warm 2700 K bulb.", kind: "learned", tags: ["learned"], at: ago(300) },
	{ id: id(), text: "Tea is oolong in the morning and rooibos after four, never with sugar.", kind: "learned", tags: ["learned", "habits"], at: ago(2900) },
	{ id: id(), text: "The bicycle is serviced every March at the shop on the corner.", kind: "memory", tags: [], at: ago(9000) },
];
const retired = [
	{ id: id(), text: "Tea is green in the morning.", kind: "learned", tags: ["learned"], at: ago(20000), retired: { at: ago(2900), why: "replaced by a newer fact", by: memories[1].id } },
];
const sources = [
	{ id: "session:garden-plan", title: "Planned the spring garden beds and the watering schedule", kind: "session", tags: ["session-note"], chunks: 3, at: ago(90) },
	{ id: "session:bread", title: "Worked out a sourdough schedule that fits a workday", kind: "session", tags: ["session-note"], chunks: 2, at: ago(700) },
	{ id: "/home/example/notes/packing-list.md", title: "Packing list for a weekend hike", kind: "file", tags: ["notes"], chunks: 1, at: ago(4000) },
	{ id: "note:1f0c", title: "Ideas for the bookshelf", kind: "note", tags: [], chunks: 1, at: ago(6000) },
	{ id: "https://example.com/articles/composting", title: "A short guide to composting at home", kind: "page", tags: ["garden"], chunks: 4, at: ago(12000) },
];
const texts = {
	"session:garden-plan": ["Goal: three raised beds by April.\n\nDecided on tomatoes, beans and herbs. Watering every second evening, more in July.", "Open question: whether the north bed gets enough sun for peppers."],
	"https://example.com/articles/composting": ["Composting turns kitchen scraps into soil. Keep a balance of green and brown material, and turn the heap every two weeks."],
};
const events = [
	{ title: "Hermes · chat", text: "Me: what should I plant next to tomatoes?\nHermes: Basil and marigolds do well beside them.", at: ago(20), kind: "chat", source: "hermes" },
	{ title: "Hourly digest", text: "Fourteen alerts this hour: mostly calendar reminders and two build results, both passing.", at: ago(55), kind: "digest", source: "digest" },
	{ title: "Calendar: Dentist at 15:00", text: "Reminder in 30 minutes.", at: ago(75), kind: "notification", source: "desktop-notification" },
	{ title: "Build finished", text: "garden-site: all 42 checks passed.", at: ago(130), kind: "notification", source: "desktop-notification" },
	{ title: "Hermes · chat", text: "Me: remind me what tea I drink in the afternoon\nHermes: Rooibos, after four.", at: ago(1500), kind: "chat", source: "hermes" },
	{ title: "Package delivered", text: "Your parcel was left at the front door.", at: ago(1700), kind: "notification", source: "desktop-notification" },
];
const tally = (rows) => { const out = {}; for (const row of rows) out[row.kind] = (out[row.kind] ?? 0) + 1; return out; };
const minutes = (since) => Number(since.slice(0, -1)) * { m: 1, h: 60, d: 1440 }[since.at(-1)];
const within = (since) => events.filter((event) => Date.now() - Date.parse(`${event.at}Z`) <= minutes(since) * 60_000);

const openNotes = [
	{ source: "session:demo-1", title: "Phone sign-in and the new Home", at: ago(300), folder: "~/Projects/recall", kind: "claude", items: [
		{ id: "open:0123456789ab", text: "Sign-in was not tested on the phone. Open the app once to confirm." },
		{ id: "open:1123456789ab", text: "The `RELEASE` setting is not shown anywhere in the app." },
		{ id: "open:2123456789ab", text: "Decide whether saved pages should keep their images." },
	] },
	{ source: "session:demo-2", title: "Chat about the weekend plans", at: ago(2000), folder: null, kind: "hermes", items: [
		{ id: "open:3123456789ab", text: "Book the table for Saturday." },
	] },
];
const closedItems = [
	{ id: "open:9123456789ab", text: "Nothing is pushed to GitHub.", how: "done", why: "The session pushed the branch and opened a pull request.",
		quote: "Pushed the branch.", by: "session:demo-1", at: ago(200), title: "Earlier work on the Home page" },
];

async function sample(method, url, body) {
	const path = url.pathname.slice("/api/v1".length);
	const q = url.searchParams;
	if (method === "GET" && path === "/open") {
		const notes = openNotes.map((n) => ({ ...n, items: n.items.filter((i) => !closedItems.some((c) => c.id === i.id)) })).filter((n) => n.items.length);
		return [200, { days: Math.min(Number(q.get("days") ?? 14), 90), total: notes.reduce((sum, n) => sum + n.items.length, 0), notes }];
	}
	if (method === "GET" && path === "/open/closed") return [200, { closed: closedItems }];
	if (method === "POST" && /^\/open\/[^/]+\/done$/.test(path)) {
		const ident = decodeURIComponent(path.split("/")[2]);
		const found = openNotes.flatMap((n) => n.items.map((i) => ({ ...i, title: n.title }))).find((i) => i.id === ident);
		if (!found) return [404, { error: `no session note of the last 90 days lists ${ident}` }];
		if (!closedItems.some((c) => c.id === ident)) closedItems.unshift({ id: ident, text: found.text, how: body?.how ?? "done", why: "", quote: "", by: "app", at: ago(0), title: found.title });
		return [200, { closed: { id: ident, text: found.text } }];
	}
	if (method === "POST" && /^\/open\/[^/]+\/reopen$/.test(path)) {
		const at = closedItems.findIndex((c) => c.id === decodeURIComponent(path.split("/")[2]));
		if (at >= 0) closedItems.splice(at, 1);
		return [200, { reopened: at >= 0 }];
	}
	if (method === "GET" && path === "/me") return [200, { user: "preview", scopes: ["recall:read", "recall:write", "recall:forget"] }];
	if (method === "GET" && path === "/status") {
		return [200, { memories: memories.length, learned: memories.filter((m) => m.kind === "learned").length, retired: retired.length,
			unread_turns: 2, last_read: ago(240), profile_written: ago(5000) }];
	}
	if (method === "GET" && path === "/search") {
		const words = (q.get("q") ?? "").toLowerCase().split(/\s+/).filter(Boolean);
		const pool = [
			...sources.map((s) => ({ source: s.id, title: s.title, kind: s.kind, tags: s.tags, text: (texts[s.id] ?? [s.title])[0], at: s.at })),
			...memories.map((m) => ({ source: m.id, title: "Memory", kind: m.kind, tags: m.tags, text: m.text, at: m.at })),
			...(q.get("events") === "1" ? events.map((e, i) => ({ source: `event:${i}`, title: e.title, kind: e.kind, tags: ["event"], text: e.text, at: e.at })) : []),
		];
		const hits = pool.map((row) => ({ ...row, id: `${row.source}#0`, chunk: 0,
			score: words.filter((w) => `${row.title} ${row.text}`.toLowerCase().includes(w)).length / Math.max(words.length, 1) }))
			.filter((row) => row.score > 0).sort((a, b) => b.score - a.score);
		return [200, { query: q.get("q"), hits }];
	}
	if (method === "GET" && path === "/memories") {
		const rows = q.get("learned") === "1" ? memories.filter((m) => m.kind === "learned") : memories;
		return [200, { memories: rows.slice(0, Number(q.get("limit") ?? 30)) }];
	}
	if (method === "POST" && path === "/memories") {
		if (typeof body?.fact !== "string" || !body.fact.trim()) return [400, { error: "fact is missing" }];
		memories.unshift({ id: id(), text: body.fact.trim(), kind: "memory", tags: [], at: ago(0) });
		return [201, { id: memories[0].id }];
	}
	if (method === "DELETE" && path.startsWith("/memories/")) {
		const at = memories.findIndex((m) => m.id === decodeURIComponent(path.slice(10)));
		if (at >= 0) memories.splice(at, 1);
		return [200, { removed: Number(at >= 0) }];
	}
	if (method === "GET" && path === "/retired") return [200, { memories: retired }];
	if (method === "POST" && /^\/retired\/.+\/restore$/.test(path)) {
		const at = retired.findIndex((m) => m.id === decodeURIComponent(path.split("/")[2]));
		if (at >= 0) { const [row] = retired.splice(at, 1); delete row.retired; memories.unshift(row); }
		return [200, { restored: Number(at >= 0) }];
	}
	if (method === "POST" && path === "/notes") {
		if (typeof body?.text !== "string" || !body.text.trim()) return [400, { error: "text is missing" }];
		const row = { id: `note:${Date.now().toString(16)}`, title: body.title || body.text.trim().split("\n")[0].slice(0, 72), kind: "note", tags: [], chunks: 1, at: ago(0) };
		sources.unshift(row);
		texts[row.id] = [body.text.trim()];
		return [201, { id: row.id, chunks: 1 }];
	}
	if (method === "GET" && path === "/sources") {
		const all = [...sources, ...memories.map((m) => ({ id: m.id, title: m.text.slice(0, 60), kind: m.kind, tags: m.tags, chunks: 1, at: m.at }))];
		return [200, { total: all.length, kinds: tally(all), sources: all.slice(0, Number(q.get("limit") ?? 100)) }];
	}
	if (method === "GET" && path === "/source") {
		const row = sources.find((s) => s.id === q.get("id")) ?? memories.find((m) => m.id === q.get("id"));
		if (!row) return [404, { error: "nothing saved under that id" }];
		return [200, { id: row.id, title: row.title ?? "Memory", kind: row.kind, tags: row.tags, at: row.at, chunks: texts[row.id] ?? [row.text ?? row.title] }];
	}
	if (method === "GET" && path === "/events/counts") {
		const rows = within(q.get("since") ?? "24h");
		return [200, { since: q.get("since") ?? "24h", total: rows.length, kinds: tally(rows) }];
	}
	if (method === "GET" && path === "/events") return [200, { events: within(q.get("since") ?? "24h") }];
	if (method === "GET" && path === "/profile") {
		return [200, { text: "Keeps a small garden and plans it in spring.\nDrinks oolong in the morning and rooibos in the afternoon.\nPrefers short, direct answers.", written: ago(5000) }];
	}
	return [404, { error: "not found" }];
}

// ----- the server ------------------------------------------------------------
const cookieOf = (req) => /(?:^|;\s*)preview=1/.test(req.headers.cookie ?? "");
const send = (res, status, type, body, extra = {}) => { res.writeHead(status, { "content-type": type, ...extra }); res.end(body); };
const json = (res, status, body, extra = {}) => send(res, status, "application/json", JSON.stringify(body), { "cache-control": "no-store", ...extra });

createServer(async (req, res) => {
	const url = new URL(req.url, `http://${req.headers.host}`);
	try {
		if (url.pathname === "/auth/login") {
			const next = url.searchParams.get("next") ?? "/";
			return send(res, 302, "text/plain", "", { location: /^\/(?!\/)/.test(next) ? next : "/", "set-cookie": "preview=1; Path=/; HttpOnly; SameSite=Lax" });
		}
		if (url.pathname === "/auth/logout" && req.method === "POST") {
			return send(res, 204, "text/plain", "", { "set-cookie": "preview=; Path=/; Max-Age=0" });
		}
		if (url.pathname.startsWith("/api/")) {
			if (req.method !== "GET" && !req.headers["x-recall-app"]) return json(res, 403, { error: "this request did not come from the recall app" });
			if (!cookieOf(req)) return json(res, 401, { error: "sign in first" });
			const chunks = [];
			for await (const chunk of req) chunks.push(chunk);
			const raw = Buffer.concat(chunks);
			if (REAL) {
				const answer = await fetch(new URL(url.pathname + url.search, REAL), {
					method: req.method, body: raw.length ? raw : undefined,
					headers: { authorization: `Bearer ${TOKEN}`, ...(raw.length ? { "content-type": "application/json" } : {}) },
				});
				return send(res, answer.status, "application/json", await answer.text(), { "cache-control": "no-store" });
			}
			const [status, body] = await sample(req.method, url, raw.length ? JSON.parse(raw) : undefined);
			return json(res, status, body);
		}
		const wanted = normalize(decodeURIComponent(url.pathname));
		const file = extname(wanted) && !wanted.includes("..") && wanted !== "/_headers" ? wanted : "/index.html";
		const extra = file.startsWith("/fonts/") ? {} : { "cache-control": "no-cache" };
		try {
			return send(res, 200, TYPES[extname(file)] ?? "application/octet-stream", await readFile(join(ROOT, file)), { ...headers, ...extra });
		} catch {
			return send(res, 404, "text/plain", "not found", headers);
		}
	} catch (error) {
		console.error(error);
		return json(res, 500, { error: "the preview server failed" });
	}
}).on("error", (error) => {
	if (error.code !== "EADDRINUSE") throw error;
	console.error(`Port ${PORT} is taken, most likely by a preview that is already running: open http://127.0.0.1:${PORT}\nFor a second one, pass another port: node preview.mjs --port ${PORT + 1}`);
	process.exit(1);
}).listen(PORT, "127.0.0.1", () => console.log(`recall app preview: http://127.0.0.1:${PORT}${REAL ? ` (API: ${REAL})` : " (sample data)"}`));
