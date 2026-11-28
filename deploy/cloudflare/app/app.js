// The recall app: search, save and look after what recall knows, from a phone or a desktop.
//
// Plain JavaScript, no build step. The pages talk to recall's web API under /api/v1 and never
// see a token: the Worker that serves these files signs the user in and keeps the tokens, and
// the browser only holds a session cookie it cannot read. Everything recall returns is put on
// the page as text, never as HTML.

const API = "/api/v1";
const DRAFT = "recall-draft"; // what was being saved when the sign-in ran out
const main = document.getElementById("main");

const KINDS = {
	memory: ["Memory", "memories"], learned: ["Learned", "learned"], session: ["Session", "sessions"],
	note: ["Note", "notes"], page: ["Page", "pages"], file: ["File", "files"],
	chat: ["Chat", "chats"], digest: ["Digest", "digests"], notification: ["Alert", "alerts"],
};
const kindOf = (kind) => (kind in KINDS ? kind : "file");
const kindName = (kind) => KINDS[kindOf(kind)][0];
const kindPlural = (kind) => KINDS[kindOf(kind)][1];
/** "1 file", "59 files" */
const kindCount = (kind, total) => `${count(total)} ${total === 1 ? kindName(kind).toLowerCase() : kindPlural(kind)}`;

// ----- small helpers ---------------------------------------------------------

/** Make an element. Text children are added as text, so nothing from recall is parsed as HTML. */
function el(tag, props, ...kids) {
	const node = document.createElement(tag);
	for (const [name, value] of Object.entries(props ?? {})) {
		if (value == null || value === false) continue;
		if (name === "class") node.className = value;
		else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
		else node.setAttribute(name, value === true ? "" : String(value));
	}
	node.append(...kids.flat().filter((kid) => kid != null && kid !== false));
	return node;
}

const count = (value) => new Intl.NumberFormat().format(value ?? 0);
const plural = (value, one, many = `${one}s`) => `${count(value)} ${value === 1 ? one : many}`;

/** recall's times are UTC; some arrive without saying so. */
function dateOf(iso) {
	if (!iso) return null;
	const date = new Date(/(?:[zZ]|[+-]\d\d:?\d\d)$/.test(iso) ? iso : `${iso}Z`);
	return Number.isNaN(date.getTime()) ? null : date;
}

function day(iso) {
	const date = dateOf(iso);
	if (!date) return "";
	const sameYear = date.getFullYear() === new Date().getFullYear();
	return date.toLocaleDateString(undefined, { month: "short", day: "numeric", ...(sameYear ? {} : { year: "numeric" }) });
}

function clock(iso) {
	const date = dateOf(iso);
	return date ? date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false }) : "";
}

function dayHeading(date) {
	const start = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
	const days = Math.round((start(new Date()) - start(date)) / 86_400_000);
	const full = date.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
	return days === 0 ? `Today · ${full}` : days === 1 ? `Yesterday · ${full}` : full;
}

// ----- messages ---------------------------------------------------------------
// A message about what just happened. Each one is its own manual popover: it sits above
// everything else and leaves only when it is closed. A confirmation is closed after a few
// seconds, because the page already shows what changed. An error stays until it is dismissed,
// so a failed action cannot go by unnoticed.

const toasts = document.getElementById("toasts");
const POPOVER = Object.hasOwn(HTMLElement.prototype, "popover"); // without it a message is a fixed box
const STAY_MS = 4000;
let toastCount = 0;

/** Say it to a screen reader: ariaNotify where the browser has it, else the page's live region. */
function announce(message, urgent) {
	if (typeof document.ariaNotify === "function") return document.ariaNotify(message, { priority: urgent ? "high" : "normal" });
	const region = document.getElementById("announce");
	document.getElementById("announce-label").hidden = false;
	region.textContent = "";
	requestAnimationFrame(() => { region.textContent = message; });
}

/** Lift each message above the ones under it. */
function stackToasts() {
	let lift = 0;
	for (const box of [...toasts.children].reverse()) {
		box.style.setProperty("--lift", `${lift}px`);
		lift += box.offsetHeight + 8;
	}
}

function toast(message, { error = false } = {}) {
	// Under the confirm dialog a message could be neither seen nor reached: show it once that closes.
	if (dialog.open) return dialog.addEventListener("close", () => toast(message, { error }), { once: true });
	if (!error) for (const old of toasts.querySelectorAll(".toast:not(.error)")) old.dispatchEvent(new Event("leave"));

	const id = `toast-${++toastCount}`;
	const from = document.activeElement; // where the focus was, to give it back
	const close = el("button", {
		class: "toast-close", type: "button", "aria-label": "Dismiss",
		...(POPOVER ? { popovertarget: id, popovertargetaction: "hide" } : {}),
	}, icon("M6 6l12 12M18 6L6 18", 16, 2));
	const box = el("div", { id, class: error ? "toast error" : "toast", ...(POPOVER ? { popover: "manual" } : {}) },
		el("p", {}, message), close);
	let timer;
	let hadFocus = false;

	const gone = () => {
		if (!box.isConnected) return;
		clearTimeout(timer);
		const next = box.previousElementSibling ?? box.nextElementSibling;
		box.remove();
		stackToasts();
		// Dismissed with the focus on it: on to the next message, or back to where the focus was.
		if (hadFocus) (next?.querySelector("button") ?? (from?.isConnected ? from : main)).focus({ preventScroll: true });
	};
	const leave = () => {
		hadFocus = box.contains(document.activeElement);
		if (POPOVER && box.matches(":popover-open")) box.hidePopover(); // its toggle event calls gone()
		else gone();
	};
	box.addEventListener("beforetoggle", (event) => { if (event.newState === "closed") hadFocus = box.contains(document.activeElement); });
	box.addEventListener("toggle", (event) => { if (event.newState === "closed") gone(); });
	box.addEventListener("leave", leave);
	if (!POPOVER) close.addEventListener("click", leave);

	toasts.append(box);
	if (POPOVER) box.showPopover();
	stackToasts();
	requestAnimationFrame(() => box.classList.add("in"));
	announce(message, error);
	if (!error) {
		// Not while it is being read or reached for: wait until the pointer and the focus have left it.
		const expire = () => {
			if (box.contains(document.activeElement) || box.matches(":hover")) timer = setTimeout(expire, 1000);
			else leave();
		};
		timer = setTimeout(expire, STAY_MS);
	}
}

/** Mark a button busy without disabling it, so it keeps the focus. Returns false when it already is. */
function busy(button, on) {
	if (on && button.getAttribute("aria-disabled") === "true") return false;
	if (on) button.setAttribute("aria-disabled", "true");
	else button.removeAttribute("aria-disabled");
	return true;
}

const wait = () => el("div", { class: "wait", "aria-hidden": "true" });

/** Bold and code marks inside one line of saved text. */
function inline(text) {
	return text.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/).filter(Boolean).map((part) => {
		if (part.length > 4 && part.startsWith("**") && part.endsWith("**")) return el("strong", {}, part.slice(2, -2));
		if (part.length > 2 && part.startsWith("`") && part.endsWith("`")) return el("code", {}, part.slice(1, -1));
		return part;
	});
}

/** Saved text is mostly Markdown. Show its headings, lists and code as such, built node by
 *  node from the text, so nothing in it is ever run or parsed as HTML. */
function rich(text) {
	const out = el("div", { class: "text" });
	let list = null;
	let code = null;
	let lines = [];
	const flush = () => {
		if (lines.length) out.append(el("p", {}, inline(lines.join("\n"))));
		lines = [];
	};
	for (const line of String(text ?? "").split("\n")) {
		const fence = /^\s*```/.test(line);
		if (code) {
			if (fence) {
				out.append(el("pre", {}, code.join("\n")));
				code = null;
			} else code.push(line);
			continue;
		}
		const heading = /^(#{1,4})\s+(.+)$/.exec(line);
		const bullet = /^\s*[-*•]\s+(.+)$/.exec(line);
		if (fence || heading || bullet || !line.trim()) flush();
		if (!bullet) list = null;
		if (fence) code = [];
		else if (heading) out.append(el(heading[1].length < 3 ? "h2" : "h3", {}, inline(heading[2])));
		else if (bullet) {
			if (!list) out.append((list = el("ul", {})));
			list.append(el("li", {}, inline(bullet[1])));
		} else if (line.trim()) lines.push(line);
	}
	if (code) out.append(el("pre", {}, code.join("\n")));
	flush();
	return out;
}

/** The same text without its marks, for a short preview. */
const plain = (text) => String(text ?? "").replace(/\*\*|`/g, "").replace(/^\s*#{1,4}\s+/gm, "");

// ----- talking to recall -----------------------------------------------------

class SignedOut extends Error {}

async function api(path, { method = "GET", body } = {}) {
	let answer;
	try {
		answer = await fetch(API + path, {
			method,
			headers: { "X-Recall-App": "1", ...(body ? { "Content-Type": "application/json" } : {}) },
			body: body ? JSON.stringify(body) : undefined,
		});
	} catch {
		throw new Error(navigator.onLine === false ? "You are offline." : "recall could not be reached.");
	}
	if (answer.status === 401) {
		signIn();
		throw new SignedOut();
	}
	const data = await answer.json().catch(() => null);
	if (!answer.ok) throw new Error(data?.error || `recall answered with an error (${answer.status}).`);
	return data ?? {};
}

function signIn() {
	turn++; // whatever was loading is no longer wanted
	document.body.classList.add("signed-out");
	document.title = "Sign in · recall";
	const next = location.pathname + location.search;
	main.replaceChildren(
		el("section", { class: "signin" },
			el("h1", {}, "Sign in to open recall"),
			el("p", { class: "muted" }, "Your knowledge base is private. Sign in to search it and save to it."),
			el("a", { class: "button primary", href: `/auth/login?next=${encodeURIComponent(next)}` }, "Sign in"),
		),
	);
}

async function signOut(event) {
	const button = event.currentTarget;
	if (!busy(button, true)) return;
	try {
		await fetch("/auth/logout", { method: "POST", headers: { "X-Recall-App": "1" } });
		signIn();
	} catch {
		toast("Could not sign out: recall could not be reached.", { error: true });
		busy(button, false);
	}
}

// ----- confirm before removing something -------------------------------------

const dialog = document.getElementById("confirm");

function ask({ title, text, yes }) {
	dialog.querySelector("#confirm-title").textContent = title;
	dialog.querySelector("#confirm-text").textContent = text;
	dialog.querySelector("#confirm-yes").textContent = yes;
	dialog.returnValue = "";
	dialog.showModal();
	return new Promise((resolve) => {
		dialog.addEventListener("close", () => {
			const answer = dialog.returnValue;
			dialog.returnValue = ""; // Esc sets none, and a stale one must not count as a yes
			resolve(answer === "yes");
		}, { once: true });
	});
}

// Tapping outside closes the dialog where the browser does not do that itself (closedby).
if (!("closedBy" in HTMLDialogElement.prototype)) {
	const outside = (event) => {
		const box = dialog.getBoundingClientRect();
		return event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom;
	};
	let pressedOutside = false;
	dialog.addEventListener("pointerdown", (event) => { pressedOutside = outside(event); });
	dialog.addEventListener("pointerup", (event) => { if (pressedOutside && outside(event)) dialog.close(); });
}

async function forget(memory, after) {
	const short = memory.text.length > 140 ? `${memory.text.slice(0, 140)}…` : memory.text;
	if (!(await ask({ title: "Forget this memory?", text: `“${short}” will be removed for good.`, yes: "Forget" }))) return;
	try {
		await api(`/memories/${encodeURIComponent(memory.id)}`, { method: "DELETE" });
		toast("Forgotten");
		after();
	} catch (error) {
		if (!(error instanceof SignedOut)) toast(error.message, { error: true });
	}
}

async function restore(memory, after) {
	try {
		await api(`/retired/${encodeURIComponent(memory.id)}/restore`, { method: "POST" });
		toast("Restored");
		after();
	} catch (error) {
		if (!(error instanceof SignedOut)) toast(error.message, { error: true });
	}
}

// ----- shared pieces ---------------------------------------------------------

const itemHref = (source) => `/item?${new URLSearchParams({ id: source })}`;

function chips(current, choices, href, label = "Show") {
	return el("nav", { class: "chips", "aria-label": label },
		choices.map(([value, name, total]) =>
			el("a", { href: href(value), "data-chip": value, "aria-current": value === current ? "true" : null },
				name, total == null ? null : el("span", { class: "count" }, count(total)))),
	);
}

function problem(error) {
	return el("div", { class: "notice", "data-error": true },
		el("p", {}, el("strong", {}, error.message)),
		el("button", { class: "button", type: "button", onclick: () => show() }, "Try again"),
	);
}

const empty = (text) => el("div", { class: "notice" }, el("p", {}, text));

function memoryRow(memory, after) {
	const retired = memory.retired;
	const tags = (memory.tags ?? []).filter((tag) => tag !== "learned");
	return el("div", { class: "row" },
		el("div", { class: "row-meta" },
			el("span", { class: `label kind-${kindOf(memory.kind)}` },
				[memory.kind === "learned" ? "Learned from a chat" : "Memory", day(memory.at)].filter(Boolean).join(" · ")),
		),
		el("p", {}, memory.text),
		retired ? el("p", { class: "small muted" },
			[`Retired ${day(retired.at)}`.trim(), retired.why].filter(Boolean).join(": ")) : null,
		tags.length ? el("div", { class: "tags" }, tags.map((tag) => el("span", {}, tag))) : null,
		el("div", { class: "row-actions" }, retired
			? el("button", { class: "button quiet", type: "button", onclick: () => restore(memory, after) }, "Restore")
			: el("button", { class: "button quiet", type: "button", onclick: () => forget(memory, after) }, "Forget")),
	);
}

// ----- home ------------------------------------------------------------------

const PLACEHOLDERS = {
	memory: "A fact to remember",
	note: "Text to find later",
};

function saveTile(saved) {
	const text = el("textarea", {
		id: "save-text", name: "text", rows: "1", placeholder: PLACEHOLDERS.memory, autocomplete: "off",
	});
	const draft = sessionStorage.getItem(DRAFT);
	if (draft) {
		text.value = draft;
		sessionStorage.removeItem(DRAFT);
	}
	const submit = el("button", { class: "submit", type: "submit" }, "Save");
	const choice = (value, name, checked) =>
		el("label", {}, el("input", { type: "radio", name: "as", value, checked }), name);

	const form = el("form", { class: "tile wide save" },
		el("h2", {}, el("label", { for: "save-text" }, "Keep a note or a fact")),
		el("div", { class: "save-row" }, text, submit),
		el("fieldset", { class: "segmented" }, el("legend", { class: "sr" }, "Save as"),
			choice("memory", "Memory", true), choice("note", "Note")),
	);
	const chosen = () => new FormData(form).get("as");

	form.addEventListener("change", () => { text.placeholder = PLACEHOLDERS[chosen()]; });
	text.addEventListener("keydown", (event) => {
		if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
			event.preventDefault();
			form.requestSubmit();
		}
	});
	form.addEventListener("submit", async (event) => {
		event.preventDefault();
		const value = text.value.trim();
		const as = chosen();
		if (!value) return text.focus();
		if (as === "memory" && value.length > 2000) return toast("A memory holds up to 2,000 characters. Save this as a note.", { error: true });
		if (!busy(submit, true)) return;
		try {
			if (as === "memory") await api("/memories", { method: "POST", body: { fact: value } });
			else await api("/notes", { method: "POST", body: { text: value } });
			text.value = "";
			toast(as === "memory" ? "Saved as a memory" : "Saved as a note");
			saved();
		} catch (error) {
			if (error instanceof SignedOut) sessionStorage.setItem(DRAFT, value);
			else toast(error.message, { error: true });
		} finally {
			busy(submit, false);
		}
	});
	return form;
}

/** The search field at the top of Home: it opens the Search page with the words typed. */
function searchBox() {
	const input = el("input", {
		type: "search", id: "home-q", name: "q", maxlength: "1000", placeholder: "Search everything you saved",
		autocomplete: "off", autocapitalize: "off", spellcheck: "false", enterkeyhint: "search",
	});
	return el("form", {
		class: "search-form home-search", role: "search",
		onsubmit: (event) => {
			event.preventDefault();
			const q = input.value.trim();
			navigate(q ? `/search?${new URLSearchParams({ q })}` : "/search");
		},
	},
	el("label", { class: "sr", for: "home-q" }, "Search recall"),
	input,
	el("button", { type: "submit", "aria-label": "Find" }, arrow()));
}

async function home({ paint, live }) {
	document.title = "recall";
	const find = searchBox();
	const foot = el("div", { class: "foot" },
		el("button", { class: "button quiet", type: "button", onclick: signOut }, "Sign out"));
	const load = async () => {
		try {
			const [status, sources, learned, events] = await Promise.all([
				api("/status"), api("/sources?limit=1"), api("/memories?learned=1&limit=2"), api("/events/counts?since=24h"),
			]);
			paint(find, el("div", { class: "bento" }, save, ...homeTiles(status, sources, learned.memories ?? [], events, load)), foot);
		} catch (error) {
			if (!(error instanceof SignedOut) && live()) paint(find, el("div", { class: "bento" }, save), problem(error), foot);
		}
	};
	const save = saveTile(load);
	paint(find, el("div", { class: "bento" }, save, wait(), wait(), el("div", { class: "wait tile wide" })));
	await load();
}

function homeTiles(status, sources, learned, events, reload) {
	const kinds = Object.entries(sources.kinds ?? {}).slice(0, 2).map(([kind, total]) => kindCount(kind, total));
	const waiting = status.unread_turns ?? 0;
	const profileLine = [
		status.profile_written ? `Profile updated ${day(status.profile_written)}` : "No profile yet",
		waiting ? `${plural(waiting, "exchange")} waiting` : "nothing waiting",
	].join(" · ");
	// Chats and digests are what is worth a look; alerts come in by the hundred, so they are shown dimmer.
	const stat = (kind, one, many, dim) => {
		const total = events.kinds?.[kind] ?? 0;
		return el("div", { class: `stat${dim ? " dim" : ""}` },
			el("p", { class: "n" }, count(total)),
			el("p", { class: "small muted" }, el("span", { class: `dot kind-${kind}`, "aria-hidden": "true" }), total === 1 ? one : many));
	};
	const short = (text) => (text.length > 60 ? `${text.slice(0, 60)}…` : text);

	return [
		el("a", { class: "tile", href: "/memories" },
			el("p", { class: "small muted" }, "Memories"),
			el("p", { class: "big" }, count(status.memories)),
			el("p", { class: "small muted" }, `${count(status.learned)} learned from chats`)),

		el("a", { class: "tile", href: "/sources" },
			el("p", { class: "small muted" }, "Sources"),
			el("p", { class: "big" }, count(sources.total)),
			el("p", { class: "small muted" }, kinds.join(" · ") || "Nothing saved yet")),

		el("section", { class: "tile wide learned", "aria-labelledby": "learned-title" },
			el("div", { class: "tile-head" },
				el("h2", { id: "learned-title" }, "Learned lately"),
				status.learned ? el("a", { href: "/memories?show=learned" }, `All ${count(status.learned)}`) : null),
			el("a", { class: "status-line", href: "/profile" }, profileLine),
			learned.length
				? learned.map((memory) => el("div", { class: "fact" },
					el("div", {},
						el("p", {}, memory.text),
						el("p", { class: "label" }, [day(memory.at), "from a chat"].filter(Boolean).join(" · "))),
					el("button", {
						class: "forget", type: "button", "aria-label": `Forget: ${short(memory.text)}`,
						onclick: () => forget(memory, reload),
					}, icon("M4.5 7h15M9.5 7V4.5h5V7M6.5 7l1 12.5h9l1-12.5", 18, 1.8))))
				: el("p", { class: "small muted fact" }, "Nothing learned from your chats yet.")),

		el("a", { class: "tile wide day-tile", href: "/timeline" },
			el("div", { class: "tile-head" },
				el("p", { class: "small muted" }, "Last 24 hours"),
				el("span", { class: "link" }, "Timeline")),
			el("div", { class: "stats" },
				stat("chat", "chat", "chats"), stat("digest", "digest", "digests"), stat("notification", "alert", "alerts", true))),
	];
}

// ----- search ----------------------------------------------------------------

async function search({ params, paint, live }) {
	const q = (params.get("q") ?? "").trim();
	const events = params.get("events") === "1";
	document.title = q ? `${q} · recall` : "Search · recall";

	const input = el("input", {
		type: "search", id: "q", name: "q", value: q, maxlength: "1000", placeholder: "Search everything you saved",
		autocomplete: "off", autocapitalize: "off", spellcheck: "false", enterkeyhint: "search",
	});
	const box = el("input", { type: "checkbox", name: "events", checked: events });
	const go = () => {
		const next = new URLSearchParams();
		if (input.value.trim()) next.set("q", input.value.trim());
		if (box.checked) next.set("events", "1");
		navigate(`/search${next.size ? `?${next}` : ""}`);
	};
	box.addEventListener("change", go);
	const results = el("div", {});

	paint(
		el("div", { class: "page-head" },
			el("h1", {}, "Search"),
			el("form", { class: "search-form", role: "search", onsubmit: (event) => { event.preventDefault(); go(); } },
				el("label", { class: "sr", for: "q" }, "Search recall"),
				input,
				el("button", { type: "submit", "aria-label": "Find" }, arrow())),
			el("label", { class: "check" }, box, "Include alerts and chats")),
		results,
	);
	if (!q) return input.focus();

	results.replaceChildren(wait());
	try {
		const query = new URLSearchParams({ q, limit: "12", ...(events ? { events: "1" } : {}) });
		const { hits = [] } = await api(`/search?${query}`);
		if (!live()) return;
		results.replaceChildren(
			el("p", { class: "label", role: "status" }, hits.length ? plural(hits.length, "result") : "Nothing found"),
			hits.length
				? el("div", { class: "list" }, hits.map((hit) =>
					el("a", { class: "row", href: itemHref(hit.source) },
						el("div", { class: "row-meta" },
							el("span", { class: `label kind-${kindOf(hit.kind)}` }, [kindName(hit.kind), day(hit.at)].filter(Boolean).join(" · ")),
							el("span", { class: "label" }, Number(hit.score ?? 0).toFixed(2))),
						el("div", { class: "row-title" }, hit.title || "Untitled"),
						el("div", { class: "row-text clamp-3" }, plain(hit.text)))))
				: empty(events ? "Try other words." : "Try other words, or include alerts and chats."),
		);
	} catch (error) {
		if (!(error instanceof SignedOut) && live()) results.replaceChildren(problem(error));
	}
}

/** A stroke icon drawn from one path; decoration only, so screen readers skip it. */
function icon(d, size = 20, weight = 2.2) {
	const NS = "http://www.w3.org/2000/svg";
	const svg = document.createElementNS(NS, "svg");
	for (const [name, value] of Object.entries({
		"aria-hidden": "true", width: size, height: size, viewBox: "0 0 24 24", fill: "none",
		stroke: "currentColor", "stroke-width": weight, "stroke-linecap": "round", "stroke-linejoin": "round",
	})) svg.setAttribute(name, value);
	const path = document.createElementNS(NS, "path");
	path.setAttribute("d", d);
	svg.append(path);
	return svg;
}

const arrow = () => icon("M5 12h14M13 6l6 6-6 6");

// ----- memories --------------------------------------------------------------

async function memories({ params, paint }) {
	const showing = ["learned", "retired"].includes(params.get("show")) ? params.get("show") : "all";
	document.title = "Memories · recall";
	const head = (status) => el("div", { class: "page-head" },
		el("h1", {}, "Memories"),
		chips(showing, [
			["all", "All", status?.memories], ["learned", "Learned from chats", status?.learned], ["retired", "Retired", status?.retired],
		], (value) => (value === "all" ? "/memories" : `/memories?show=${value}`)));
	paint(head(), wait(), wait());

	const load = async () => {
		const [status, data] = await Promise.all([
			api("/status"),
			showing === "retired" ? api("/retired?limit=100") : api(`/memories?limit=200${showing === "learned" ? "&learned=1" : ""}`),
		]);
		const rows = data.memories ?? [];
		const none = {
			all: "No memories yet. Save a fact from Home and it shows up here.",
			learned: "Nothing learned from your chats yet.",
			retired: "Nothing retired. A memory is retired when a newer fact replaces it.",
		}[showing];
		paint(head(status), rows.length ? el("div", { class: "list" }, rows.map((memory) => memoryRow(memory, reload))) : empty(none));
	};
	const reload = () => load().catch((error) => { if (!(error instanceof SignedOut)) toast(error.message, { error: true }); });
	await load();
}

// ----- timeline --------------------------------------------------------------

async function timeline({ params, paint }) {
	const showing = ["chat", "digest", "notification"].includes(params.get("show")) ? params.get("show") : "all";
	const since = params.get("since") === "7d" ? "7d" : "24h";
	document.title = "Timeline · recall";
	const href = (show, window = since) => {
		const next = new URLSearchParams();
		if (show !== "all") next.set("show", show);
		if (window !== "24h") next.set("since", window);
		return `/timeline${next.size ? `?${next}` : ""}`;
	};
	const title = el("h1", {}, since === "7d" ? "Last 7 days" : "Last 24 hours");
	paint(el("div", { class: "page-head" }, title), wait(), wait());

	const LIMIT = 500;
	const { events = [] } = await api(`/events?since=${since}&limit=${LIMIT}`);
	const totals = Object.groupBy(events, (event) => kindOf(event.kind));
	const shown = showing === "all" ? events : (totals[showing] ?? []);

	const list = [];
	let heading = "";
	for (const event of shown) {
		const date = dateOf(event.at);
		const label = date ? dayHeading(date) : "";
		if (label !== heading) {
			heading = label;
			list.push(el("h2", { class: "label day" }, label));
		}
		list.push(el("details", { class: "row" },
			el("summary", {},
				el("span", { class: "time" }, clock(event.at)),
				el("span", {},
					el("span", { class: `label kind-${kindOf(event.kind)}` }, kindName(event.kind)),
					el("span", { class: "row-title clamp-2" }, event.title || "Untitled"))),
			el("div", { class: "full" }, event.text)));
	}

	paint(
		el("div", { class: "page-head" }, title,
			chips(showing, [
				["all", "All", events.length], ["chat", "Chats", totals.chat?.length ?? 0],
				["digest", "Digests", totals.digest?.length ?? 0], ["notification", "Alerts", totals.notification?.length ?? 0],
			], (value) => href(value))),
		shown.length ? el("div", { class: "list" }, list) : empty("Nothing came in during this time."),
		el("div", { class: "foot" },
			el("a", { class: "button quiet", href: href(showing, since === "7d" ? "24h" : "7d") },
				since === "7d" ? "Show the last 24 hours" : "Show the last 7 days"),
			events.length === LIMIT ? el("span", { class: "small muted" }, `Showing the newest ${count(LIMIT)}.`) : null),
	);
}

// ----- sources ---------------------------------------------------------------

async function sources({ params, paint }) {
	document.title = "Sources · recall";
	paint(el("div", { class: "page-head" }, el("h1", {}, "Sources")), wait(), wait());

	const data = await api("/sources?limit=1000");
	const rows = data.sources ?? [];
	const kinds = Object.entries(data.kinds ?? {});
	const showing = kinds.some(([kind]) => kind === params.get("show")) ? params.get("show") : "all";
	const shown = showing === "all" ? rows : rows.filter((row) => row.kind === showing);

	paint(
		el("div", { class: "page-head" },
			el("h1", {}, "Sources"),
			chips(showing, [["all", "All", data.total], ...kinds.map(([kind, total]) => [kind, kindPlural(kind).replace(/^./, (c) => c.toUpperCase()), total])],
				(value) => (value === "all" ? "/sources" : `/sources?show=${value}`))),
		shown.length
			? el("div", { class: "list" }, shown.map((row) =>
				el("a", { class: "row", href: itemHref(row.id) },
					el("div", { class: "row-meta" },
						el("span", { class: `label kind-${kindOf(row.kind)}` }, [kindName(row.kind), day(row.at)].filter(Boolean).join(" · ")),
						row.chunks > 1 ? el("span", { class: "label" }, `${count(row.chunks)} parts`) : null),
					el("div", { class: "row-title" }, row.title || row.id))))
			: empty("Nothing saved yet."),
		rows.length < (data.total ?? 0) ? el("p", { class: "small muted foot" }, `Showing the newest ${count(rows.length)} of ${count(data.total)}.`) : null,
	);
}

// ----- one item --------------------------------------------------------------

async function item({ params, paint }) {
	const id = params.get("id") ?? "";
	document.title = "recall";
	const back = el("button", { class: "button quiet", type: "button", onclick: () => (depth > 0 ? history.back() : navigate("/")) }, "← Back");
	paint(el("div", { class: "item" }, el("div", {}, back), wait()));

	const data = await api(`/source?${new URLSearchParams({ id })}`);
	document.title = `${data.title || "Item"} · recall`;
	const web = /^https?:\/\//i.test(data.id) && URL.canParse(data.id) ? data.id : null;
	const tags = (data.tags ?? []).filter((tag) => !["memory", "event"].includes(tag));
	const texts = data.chunks ?? [];
	paint(el("article", { class: "item" },
		el("div", {}, back),
		el("p", { class: `label kind-${kindOf(data.kind)}` }, [kindName(data.kind), day(data.at)].filter(Boolean).join(" · ")),
		el("h1", {}, data.title || "Untitled"),
		tags.length ? el("div", { class: "tags" }, tags.map((tag) => el("span", {}, tag))) : null,
		web ? el("p", {}, el("a", { class: "button", href: web, target: "_blank", rel: "noopener noreferrer" }, "Open the original")) : null,
		texts.map((text) => rich(text)),
		texts.length >= 200 ? el("p", { class: "small muted" }, "Showing the first 200 parts.") : null,
	));
}

// ----- open items ------------------------------------------------------------
// What sessions left open and nothing has closed. Done takes an item off the list on the
// server; the row stays where it is with Undo until the page is left, and the Closed list
// keeps every closure with Reopen, so nothing depends on a message that disappears.

const CLOSED_BY = { hand: "by hand", app: "in the app", assistant: "through an assistant" };
// The kinds an item can have, in the order they are offered. "step" is what only you can do.
const OPEN_KINDS = [
	["step", "Yours"], ["todo", "Unfinished"], ["waiting", "Waiting"], ["check", "Unverified"], ["idea", "Ideas"], ["other", "Not sorted"],
];
const openKindName = (kind) => OPEN_KINDS.find(([value]) => value === kind)?.[1] ?? "Not sorted";

async function openItems({ params, paint }) {
	const showing = params.get("show") === "closed" ? "closed" : "open";
	const days = params.get("days") === "90" ? 90 : 14;
	document.title = `${showing === "closed" ? "Closed" : "Open"} items · recall`;
	const head = (total) => el("div", { class: "page-head" },
		el("h1", {}, "Open items"),
		chips(showing, [["open", "Open", total], ["closed", "Closed"]], (value) => (value === "open" ? "/open" : "/open?show=closed")));
	paint(head(), wait(), wait());

	if (showing === "closed") {
		const load = async () => {
			const [open, data] = await Promise.all([api("/open"), api("/open/closed?limit=60")]);
			const rows = data.closed ?? [];
			paint(head(open.total), rows.length
				? el("div", { class: "list" }, rows.map((row) => closedRow(row, reload)))
				: empty("Nothing has been closed yet. An item leaves the list when a later session settles it, or when you mark it done."));
		};
		const reload = () => load().catch((error) => { if (!(error instanceof SignedOut)) toast(error.message, { error: true }); });
		return load();
	}

	const data = await api(`/open?days=${days}`);
	const tally = { ...(data.kinds ?? {}) };
	let total = data.total ?? 0;
	// Your own steps first: they are what the list is for. Everything else is one tap away.
	const asked = params.get("kind");
	const kind = asked === "all" || OPEN_KINDS.some(([value]) => value === asked) ? asked : (tally.step ? "step" : "all");
	const link = (to, span = days) => {
		const query = new URLSearchParams();
		if (to !== "step") query.set("kind", to);
		if (span !== 14) query.set("days", String(span));
		return query.size ? `/open?${query}` : "/open";
	};
	const kinds = () => chips(kind, [
		...OPEN_KINDS.filter(([value]) => tally[value] || value === kind).map(([value, name]) => [value, name, tally[value] ?? 0]),
		["all", "All", total],
	], (value) => link(value), "Kind");
	const setCount = (chip, value) => {
		const shown = main.querySelector(`.chips a[data-chip="${chip}"] .count`);
		if (shown) shown.textContent = count(value);
	};
	const changed = (by, of) => {
		total += by;
		tally[of] = (tally[of] ?? 0) + by;
		setCount("open", total);
		setCount("all", total);
		setCount(of, tally[of]);
	};
	const notes = (data.notes ?? [])
		.map((note) => ({ ...note, items: note.items.filter((item) => kind === "all" || (item.kind ?? "other") === kind) }))
		.filter((note) => note.items.length);
	const other = days === 14
		? el("a", { href: link(kind, 90) }, "Show the last 90 days")
		: el("a", { href: link(kind, 14) }, "Show the last 14 days");
	paint(head(total),
		total ? kinds() : null,
		notes.length
			? el("div", { class: "open-notes" }, notes.map((note) => openNote(note, changed, kind === "all")))
			: empty(total ? "Nothing of this kind is open." : `Nothing is open from the last ${data.days ?? days} days.`),
		el("p", { class: "small muted open-foot" }, `From the sessions of the last ${data.days ?? days} days. `, other));
}

function openNote(note, changed, labelled) {
	const where = note.folder ?? (note.kind === "hermes" ? "Hermes" : null);
	return el("div", { class: "open-note" },
		el("p", { class: "label kind-session" }, [day(note.at), where].filter(Boolean).join(" · ")),
		el("h2", {}, el("a", { href: itemHref(note.source) }, note.title)),
		el("ul", { class: "open-list", role: "list" }, note.items.map((item) => openRow(item, changed, labelled))));
}

function openRow(item, changed, labelled) {
	const textId = `open-${item.id.replace(/[^0-9a-z]/g, "-")}`;
	const button = el("button", { class: "button quiet", type: "button", "aria-describedby": textId }, "Done");
	const row = el("li", { class: "open-row" },
		el("div", {},
			el("p", { id: textId }, inline(item.text)),
			labelled ? el("p", { class: "small muted" }, openKindName(item.kind)) : null),
		button);
	button.addEventListener("click", async () => {
		if (!busy(button, true)) return;
		const closing = !row.classList.contains("closed");
		try {
			await api(`/open/${encodeURIComponent(item.id)}/${closing ? "done" : "reopen"}`, { method: "POST" });
			row.classList.toggle("closed", closing);
			button.textContent = closing ? "Undo" : "Done";
			changed(closing ? -1 : 1, item.kind ?? "other");
			toast(closing ? "Marked done" : "Back on the list");
		} catch (error) {
			if (!(error instanceof SignedOut)) toast(error.message, { error: true });
		} finally {
			busy(button, false);
		}
	});
	return row;
}

function closedRow(row, after) {
	const how = { done: "Done", dropped: "Dropped", carried: "Carried forward" }[row.how] ?? "Closed";
	const session = row.by?.startsWith("session:");
	const textId = `closed-${row.id.replace(/[^0-9a-z]/g, "-")}`;
	const reopen = async (event) => {
		const button = event.currentTarget;
		if (!busy(button, true)) return;
		try {
			await api(`/open/${encodeURIComponent(row.id)}/reopen`, { method: "POST" });
			toast("Back on the list");
			after();
		} catch (error) {
			busy(button, false);
			if (!(error instanceof SignedOut)) toast(error.message, { error: true });
		}
	};
	return el("div", { class: "row" },
		el("div", { class: "row-meta" },
			el("span", { class: "label kind-session" }, [how, day(row.at)].filter(Boolean).join(" · "))),
		el("p", { id: textId }, inline(row.text)),
		row.why ? el("p", { class: "small muted" }, row.why) : null,
		el("p", { class: "small muted" },
			session ? ["Closed by a later session: ", el("a", { href: itemHref(row.by) }, "open its note")] : `Closed ${CLOSED_BY[row.by] ?? "by hand"}`,
			row.title ? ` · from “${row.title}”` : null),
		el("div", { class: "row-actions" },
			el("button", { class: "button quiet", type: "button", "aria-describedby": textId, onclick: reopen }, "Reopen")));
}

// ----- profile ---------------------------------------------------------------

async function profile({ paint }) {
	document.title = "Profile · recall";
	paint(el("div", { class: "page-head" }, el("h1", {}, "What recall has learned")), wait());

	const [doc, status] = await Promise.all([api("/profile"), api("/status")]);
	const waiting = status.unread_turns ?? 0;
	paint(el("article", { class: "item" },
		el("p", { class: "label" }, doc.written ? `Profile · written ${day(doc.written)}` : "Profile"),
		el("h1", {}, "What recall has learned"),
		doc.text
			? rich(doc.text)
			: empty("No profile yet. recall writes one after it has read a few of your Hermes chats."),
		el("p", { class: "small muted" },
			waiting ? `${plural(waiting, "exchange")} waiting to be read.` : "Every chat has been read.",
			status.last_read ? ` Last read ${day(status.last_read)} at ${clock(status.last_read)}.` : ""),
		chips("", [["learned", "Learned facts", status.learned], ["retired", "Retired", status.retired]], (value) => `/memories?show=${value}`),
	));
}

// ----- moving between pages --------------------------------------------------

const VIEWS = {
	"/": home, "/search": search, "/memories": memories, "/timeline": timeline,
	"/sources": sources, "/item": item, "/profile": profile, "/open": openItems,
};
const TABS = { "/": "home", "/search": "search", "/memories": "memories", "/timeline": "timeline", "/open": "open" };
let turn = 0; // which page load is current; an older one must not paint
let depth = 0; // pages opened inside the app, so Back knows whether there is one to go back to

async function show({ focus = false } = {}) {
	const mine = ++turn;
	const url = new URL(location.href);
	const view = VIEWS[url.pathname];
	const live = () => mine === turn;
	const paint = (...nodes) => { if (live()) main.replaceChildren(...nodes.filter(Boolean)); };
	document.body.classList.remove("signed-out");
	for (const tab of document.querySelectorAll(".tabs a")) {
		if (tab.dataset.tab === TABS[url.pathname]) tab.setAttribute("aria-current", "page");
		else tab.removeAttribute("aria-current");
	}
	if (focus) main.focus({ preventScroll: true });
	if (!view) {
		document.title = "Not found · recall";
		return paint(el("div", { class: "notice" }, el("p", {}, el("strong", {}, "There is nothing at this address.")), el("a", { class: "button", href: "/" }, "Go to Home")));
	}
	try {
		await view({ params: url.searchParams, paint, live });
	} catch (error) {
		if (!(error instanceof SignedOut)) paint(problem(error));
	}
}

function navigate(href) {
	history.pushState({}, "", href);
	depth++;
	scrollTo(0, 0);
	show({ focus: true });
}

document.addEventListener("click", (event) => {
	if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
	const link = event.target.closest?.("a[href]");
	if (!link || link.target) return;
	if (link.classList.contains("skip")) {
		event.preventDefault();
		return main.focus();
	}
	const to = new URL(link.href, location.href);
	if (to.origin !== location.origin || !(to.pathname in VIEWS)) return; // sign-in and outside links load normally
	event.preventDefault();
	if (to.href === location.href) show();
	else navigate(to.pathname + to.search);
});

addEventListener("popstate", () => {
	depth = Math.max(0, depth - 1);
	show();
});

// Back online: reload a page that failed to load.
addEventListener("online", () => { if (main.querySelector("[data-error]")) show(); });

// The one place a script's address is handed to the browser. The app's headers require Trusted
// Types, so the address has to come from a named policy, and this policy knows a single one.
const WORKER = "/sw.js";
const scripts = (window.trustedTypes ?? { createPolicy: (name, rules) => rules }).createPolicy("recall", {
	createScriptURL(url) {
		if (url !== WORKER) throw new TypeError(`not a script of this app: ${url}`);
		return url;
	},
});
if ("serviceWorker" in navigator) {
	try {
		navigator.serviceWorker.register(scripts.createScriptURL(WORKER)).catch(() => {});
	} catch {
		// The app works without its offline copy.
	}
}

show();
