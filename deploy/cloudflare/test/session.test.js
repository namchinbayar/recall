// Run with: node --test deploy/cloudflare/test/*.js
import assert from "node:assert/strict";
import { test } from "node:test";

import { APP_HEADER, COOKIE, EXCHANGE_PATH, LOGIN_COOKIE, Sessions, Unavailable, challengeFor, cookiesOf, safeNext } from "../src/session.js";

const ISSUER = "https://auth.example/v1/apps/P1";
const RESOURCE = "https://recall.example/mcp";
const ORIGIN = "https://recall.example";
const HOUR = 60 * 60 * 1000;
const APP_REDIRECT = "com.example.recall://signed-in";
const DAY = 24 * HOUR;

class Store {
	data = new Map();
	async get(key) {
		return structuredClone(this.data.get(key));
	}
	async put(key, value) {
		this.data.set(key, structuredClone(value));
	}
	async delete(key) {
		return this.data.delete(key);
	}
	async list({ prefix }) {
		return new Map([...this.data].filter(([key]) => key.startsWith(prefix)));
	}
}

// A stand-in for the authorization server. Refresh tokens work once, as with rotation.
function setup({ allowed = async () => "U-me", appRedirect = APP_REDIRECT } = {}) {
	const world = { store: new Store(), now: 1_000_000_000_000, calls: [], refreshTokens: new Set(), issued: 0, revoked: [] };
	world.fetch = async (url, init = {}) => {
		const path = new URL(url).pathname;
		const form = new URLSearchParams(init.body ?? "");
		world.calls.push({ path, form });
		if (path.endsWith("/.well-known/oauth-authorization-server")) {
			return Response.json({
				authorization_endpoint: "https://auth.example/authorize",
				token_endpoint: "https://auth.example/token",
				revocation_endpoint: "https://auth.example/revoke",
			});
		}
		if (path === "/revoke") {
			world.revoked.push(form.get("token"));
			return new Response(null, { status: 200 });
		}
		if (path === "/token") {
			if (world.down) return new Response("try later", { status: world.down });
			const grant = form.get("grant_type");
			if (grant === "authorization_code") {
				if (form.get("code") !== "good-code" || (await challengeFor(form.get("code_verifier"))) !== world.challenge) {
					return Response.json({ error: "invalid_grant" }, { status: 400 });
				}
			} else if (!world.refreshTokens.delete(form.get("refresh_token"))) {
				return Response.json({ error: "invalid_grant" }, { status: 400 });
			}
			const n = ++world.issued;
			world.refreshTokens.add(`refresh-${n}`);
			return Response.json({ access_token: `access-${n}`, refresh_token: `refresh-${n}`, expires_in: 3600 });
		}
		return new Response("not found", { status: 404 });
	};
	world.sessions = new Sessions({
		store: world.store, issuer: ISSUER + "/", resource: RESOURCE, allowed, appRedirect, fetch: world.fetch, now: () => world.now,
	});
	return world;
}

const get = (path, cookie) => new Request(ORIGIN + path, { headers: cookie ? { cookie } : {} });
const cookieValue = (response, name) =>
	response.headers.getSetCookie().map((line) => line.split(";")[0].split("=")).find(([key]) => key === name)?.[1];

// Go through the whole sign-in; returns the session cookie the browser ends up with.
async function signIn(world, next = "/") {
	const start = await world.sessions.handle(get(`/auth/login?next=${encodeURIComponent(next)}`));
	const to = new URL(start.headers.get("location"));
	world.challenge = to.searchParams.get("code_challenge");
	const state = to.searchParams.get("state");
	const back = await world.sessions.handle(get(`/auth/callback?code=good-code&state=${state}`, `${LOGIN_COOKIE}=${state}`));
	return { start, to, back, id: cookieValue(back, COOKIE) };
}

test("the client document names this site and asks for no secret", async () => {
	const { sessions } = setup();
	const resp = await sessions.handle(get("/oauth/client.json"));
	const doc = await resp.json();
	assert.equal(doc.client_id, `${ORIGIN}/oauth/client.json`);
	assert.deepEqual(doc.redirect_uris, [`${ORIGIN}/auth/callback`]);
	assert.equal(doc.token_endpoint_auth_method, "none");
	assert.deepEqual(doc.grant_types, ["authorization_code", "refresh_token"]);
});

test("signing in sends the browser to the authorization server with PKCE and comes back with a session", async () => {
	const world = setup();
	const { start, to, back, id } = await signIn(world, "/memories?x=1");
	assert.equal(start.status, 302);
	assert.equal(to.origin + to.pathname, "https://auth.example/authorize");
	assert.equal(to.searchParams.get("client_id"), `${ORIGIN}/oauth/client.json`);
	assert.equal(to.searchParams.get("redirect_uri"), `${ORIGIN}/auth/callback`);
	assert.equal(to.searchParams.get("resource"), RESOURCE);
	assert.equal(to.searchParams.get("code_challenge_method"), "S256");
	assert.match(to.searchParams.get("scope"), /recall:read .*offline_access/);
	assert.match(start.headers.getSetCookie()[0], /^__Host-recall-login=.*; Path=\/; Secure; HttpOnly; SameSite=Lax/);

	assert.equal(back.status, 302);
	assert.equal(back.headers.get("location"), "/memories?x=1");
	assert.match(back.headers.getSetCookie().find((c) => c.startsWith(COOKIE + "=")), /Secure; HttpOnly; SameSite=Lax; Max-Age=2592000/);
	const session = await world.store.get(`session:${id}`);
	assert.deepEqual([session.access, session.refresh, session.user], ["access-1", "refresh-1", "U-me"]);
	assert.equal((await world.store.list({ prefix: "login:" })).size, 0); // a sign-in cannot be replayed
	const exchange = world.calls.find((c) => c.path === "/token").form;
	assert.equal(exchange.get("client_id"), `${ORIGIN}/oauth/client.json`);
	assert.equal(exchange.get("resource"), RESOURCE);
	assert.equal(exchange.has("client_secret"), false);
});

test("a callback from another browser, a second use, or a late one is refused", async () => {
	const world = setup();
	const start = await world.sessions.handle(get("/auth/login"));
	const to = new URL(start.headers.get("location"));
	world.challenge = to.searchParams.get("code_challenge");
	const state = to.searchParams.get("state");
	const other = await world.sessions.handle(get(`/auth/callback?code=good-code&state=${state}`)); // no cookie
	assert.equal(other.status, 400);
	const again = await world.sessions.handle(get(`/auth/callback?code=good-code&state=${state}`, `${LOGIN_COOKIE}=${state}`));
	assert.equal(again.status, 400); // the first attempt used the state up
	const second = await world.sessions.handle(get("/auth/login"));
	const lateState = new URL(second.headers.get("location")).searchParams.get("state");
	world.now += 11 * 60 * 1000;
	const late = await world.sessions.handle(get(`/auth/callback?code=good-code&state=${lateState}`, `${LOGIN_COOKIE}=${lateState}`));
	assert.equal(late.status, 400);
	assert.equal((await world.store.list({ prefix: "session:" })).size, 0);
});

test("an account recall does not accept gets no session", async () => {
	const world = setup({ allowed: async () => null });
	const { back, id } = await signIn(world);
	assert.equal(back.status, 403);
	assert.equal(id, undefined);
	assert.equal((await world.store.list({ prefix: "session:" })).size, 0);
});

test("a refused code gets no session", async () => {
	const world = setup();
	const start = await world.sessions.handle(get("/auth/login"));
	const state = new URL(start.headers.get("location")).searchParams.get("state");
	world.challenge = "someone-else's";
	const back = await world.sessions.handle(get(`/auth/callback?code=good-code&state=${state}`, `${LOGIN_COOKIE}=${state}`));
	assert.equal(back.status, 502);
});

test("a request with the session cookie gets the token; one without, or with a made-up id, gets nothing", async () => {
	const world = setup();
	const { id } = await signIn(world);
	const found = await world.sessions.access(get("/api/v1/me", `${COOKIE}=${id}`));
	assert.deepEqual([found.token, found.user, found.cookie], ["access-1", "U-me", undefined]);
	assert.equal(await world.sessions.access(get("/api/v1/me")), null);
	assert.equal(await world.sessions.access(get("/api/v1/me", `${COOKIE}=${"x".repeat(43)}`)), null);
	assert.equal(await world.sessions.access(get("/api/v1/me", `${COOKIE}=../../etc`)), null);
});

test("a token about to expire is refreshed once, even for requests that arrive together", async () => {
	const world = setup();
	const { id } = await signIn(world);
	world.now += HOUR - 30_000;
	const request = () => world.sessions.access(get("/api/v1/me", `${COOKIE}=${id}`));
	const [a, b, c] = await Promise.all([request(), request(), request()]);
	assert.deepEqual([a.token, b.token, c.token], ["access-2", "access-2", "access-2"]);
	assert.equal(world.calls.filter((call) => call.form.get("grant_type") === "refresh_token").length, 1);
	assert.equal((await world.store.get(`session:${id}`)).refresh, "refresh-2"); // the rotated one is kept
});

test("when the refresh is refused the session ends", async () => {
	const world = setup();
	const { id } = await signIn(world);
	world.refreshTokens.clear();
	world.now += HOUR;
	assert.equal(await world.sessions.access(get("/api/v1/me", `${COOKIE}=${id}`)), null);
	assert.equal(await world.store.get(`session:${id}`), undefined);
});

test("when the sign-in service fails to answer, the session is kept", async () => {
	const world = setup();
	const { id } = await signIn(world);
	const request = () => world.sessions.access(get("/api/v1/me", `${COOKIE}=${id}`));
	world.down = 503;
	world.now += HOUR - 30_000; // about to expire: the token in hand is still used
	assert.equal((await request()).token, "access-1");
	world.now += 60_000; // expired: nothing to answer with, but not signed out either
	await assert.rejects(request(), Unavailable);
	assert.equal((await world.store.get(`session:${id}`)).refresh, "refresh-1");
	world.down = 0;
	assert.equal((await request()).token, "access-2");
});

test("use is recorded hourly and renews the cookie; a month unused ends the session", async () => {
	const world = setup();
	const { id } = await signIn(world);
	world.now += 2 * HOUR;
	const later = await world.sessions.access(get("/api/v1/me", `${COOKIE}=${id}`));
	assert.match(later.cookie, /^__Host-recall=.*Max-Age=2592000$/);
	assert.equal((await world.store.get(`session:${id}`)).used, world.now);
	world.now += 31 * DAY;
	assert.equal(await world.sessions.access(get("/api/v1/me", `${COOKIE}=${id}`)), null);
	assert.equal(await world.store.get(`session:${id}`), undefined);
});

test("signing out ends the session, revokes its refresh token and clears the cookie", async () => {
	const world = setup();
	const { id } = await signIn(world);
	const out = await world.sessions.handle(new Request(ORIGIN + "/auth/logout", { method: "POST", headers: { cookie: `${COOKIE}=${id}` } }));
	assert.equal(out.status, 204);
	assert.match(out.headers.get("set-cookie"), /^__Host-recall=; .*Max-Age=0$/);
	assert.equal(await world.store.get(`session:${id}`), undefined);
	assert.deepEqual(world.revoked, ["refresh-1"]);
	assert.equal(await world.sessions.access(get("/api/v1/me", `${COOKIE}=${id}`)), null);
});

test("only the app's own pages may change things", () => {
	const { sessions } = setup();
	const post = (headers) => sessions.fromApp(new Request(ORIGIN + "/api/v1/notes", { method: "POST", headers }));
	assert.equal(sessions.fromApp(get("/api/v1/search?q=x")), true);
	assert.equal(post({ [APP_HEADER]: "1", origin: ORIGIN }), true);
	assert.equal(post({ [APP_HEADER]: "1" }), true);
	assert.equal(post({ origin: ORIGIN }), false); // a plain form post carries no such header
	assert.equal(post({ [APP_HEADER]: "1", origin: "https://evil.example" }), false);
});

test("after signing in the browser only ever goes to a page of this site", () => {
	assert.equal(safeNext("/memories?tag=a#top"), "/memories?tag=a#top");
	for (const bad of [null, "", "memories", "//evil.example", "/\\evil.example", "https://evil.example", "/a\r\nSet-Cookie: x=1", "/auth/login", "/api/v1/me"]) {
		assert.equal(safeNext(bad), "/");
	}
});

test("ended sessions and unfinished sign-ins are cleared out", async () => {
	const world = setup();
	const { id } = await signIn(world);
	await world.sessions.handle(get("/auth/login")); // never finished
	const fresh = await signIn(world);
	world.now += 20 * DAY;
	await world.sessions.access(get("/api/v1/me", `${COOKIE}=${fresh.id}`)); // still in use
	world.now += 15 * DAY;
	assert.equal(await world.sessions.sweep(), 2);
	assert.equal(await world.store.get(`session:${id}`), undefined);
	assert.notEqual(await world.store.get(`session:${fresh.id}`), undefined);
});

test("cookies are read by name", () => {
	assert.deepEqual(cookiesOf(get("/", "a=1; __Host-recall=abc=; b")), { a: "1", "__Host-recall": "abc=" });
});

// ----- the iPhone app ----------------------------------------------------------

const APP_VERIFIER = "v".repeat(20) + "-app-verifier-0123456789abcdef";

const exchange = (body, headers = { [APP_HEADER]: "ios" }) =>
	new Request(ORIGIN + EXCHANGE_PATH, { method: "POST", headers: { "content-type": "application/json", ...headers }, body: JSON.stringify(body) });

// The app's sign-in up to the one-time code the sheet comes back with.
async function appSignIn(world, verifier = APP_VERIFIER) {
	const start = await world.sessions.handle(get(`/auth/login?app=${await challengeFor(verifier)}`));
	const to = new URL(start.headers.get("location"));
	world.challenge = to.searchParams.get("code_challenge");
	const state = to.searchParams.get("state");
	const back = await world.sessions.handle(get(`/auth/callback?code=good-code&state=${state}`, `${LOGIN_COOKIE}=${state}`));
	return { start, to, back, code: new URL(back.headers.get("location")).searchParams.get("code") };
}

test("the app's sign-in comes back to the app with a one-time code, not a session", async () => {
	const world = setup();
	const { to, back, code } = await appSignIn(world);
	assert.notEqual(to.searchParams.get("code_challenge"), await challengeFor(APP_VERIFIER)); // the Worker's own PKCE, not the app's
	assert.equal(back.status, 302);
	assert.ok(back.headers.get("location").startsWith(APP_REDIRECT + "?code="));
	assert.equal(cookieValue(back, COOKIE), undefined);
	assert.match(code, /^[A-Za-z0-9_-]{40,}$/);
});

test("without an address to return to, the app's sign-in is off and the web sign-in is not", async () => {
	for (const appRedirect of ["", null, "not an address", "com.example.recall://x y"]) {
		const world = setup({ appRedirect });
		const start = await world.sessions.handle(get(`/auth/login?app=${await challengeFor(APP_VERIFIER)}`));
		assert.equal(start.status, 404);
		assert.equal([...world.store.data.keys()].length, 0); // nothing was started
		assert.equal((await signIn(world)).id.length > 20, true);
	}
});

test("the code and the app's verifier give the session once; the session then works like the web app's", async () => {
	const world = setup();
	const { code } = await appSignIn(world);
	const resp = await world.sessions.handle(exchange({ code, verifier: APP_VERIFIER }));
	assert.equal(resp.status, 200);
	const { session, cookie } = await resp.json();
	assert.equal(cookie, COOKIE);
	const access = await world.sessions.access(get("/api/v1/me", `${COOKIE}=${session}`));
	assert.equal(access.token, "access-1");
	assert.equal(access.user, "U-me");
	const again = await world.sessions.handle(exchange({ code, verifier: APP_VERIFIER }));
	assert.equal(again.status, 400);
});

test("a code with the wrong verifier is refused and its session ended", async () => {
	const world = setup();
	const { code } = await appSignIn(world);
	const resp = await world.sessions.handle(exchange({ code, verifier: "w".repeat(43) }));
	assert.equal(resp.status, 400);
	assert.equal((await world.store.list({ prefix: "session:" })).size, 0);
	assert.equal((await world.sessions.handle(exchange({ code, verifier: APP_VERIFIER }))).status, 400);
});

test("a late code, one without the app's header, or one sent from a web page is refused", async () => {
	const world = setup();
	const late = await appSignIn(world);
	world.now += 3 * 60 * 1000;
	assert.equal((await world.sessions.handle(exchange({ code: late.code, verifier: APP_VERIFIER }))).status, 400);

	const plain = await appSignIn(world);
	assert.equal((await world.sessions.handle(exchange({ code: plain.code, verifier: APP_VERIFIER }, {}))).status, 403);
	const page = exchange({ code: plain.code, verifier: APP_VERIFIER }, { [APP_HEADER]: "ios", origin: "https://evil.example" });
	assert.equal((await world.sessions.handle(page)).status, 403);
	assert.equal((await world.sessions.handle(exchange({ code: plain.code, verifier: APP_VERIFIER }))).status, 200); // untouched by the refusals
});

test("an app sign-in with a malformed challenge does not start", async () => {
	const world = setup();
	const resp = await world.sessions.handle(get("/auth/login?app=short"));
	assert.equal(resp.status, 400);
	assert.equal((await world.store.list({ prefix: "login:" })).size, 0);
});

test("a code the app never collects is cleared out with its session", async () => {
	const world = setup();
	await appSignIn(world);
	world.now += 5 * 60 * 1000;
	assert.equal(await world.sessions.sweep(), 1);
	assert.equal((await world.store.list({ prefix: "handoff:" })).size, 0);
	assert.equal((await world.store.list({ prefix: "session:" })).size, 0);
});
