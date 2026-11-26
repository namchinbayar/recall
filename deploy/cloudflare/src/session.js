// Sign-in for the recall app.
//
// The app's pages never see an access token. This module makes the Worker the OAuth client:
// it sends the browser to the authorization server (Descope), exchanges the code it comes back
// with (PKCE, no client secret), and keeps the tokens in the Durable Object's storage. The
// browser gets only a random session id in an HttpOnly cookie. For each /api request the
// Worker looks the session up, refreshes the access token when it is about to expire, and
// adds it to the request it forwards to recall, which checks it as it checks every token.
//
// The client is identified by a Client ID Metadata Document: the address of a small JSON file
// this Worker serves (CLIENT_PATH). Nothing is registered by hand and there is no secret.
//
// Everything outside (storage, fetch, the clock) is passed in, so this file runs under
// `node --test` as it does on Cloudflare.

export const COOKIE = "__Host-recall";
export const LOGIN_COOKIE = "__Host-recall-login";
export const CLIENT_PATH = "/oauth/client.json";
export const CALLBACK_PATH = "/auth/callback";
export const APP_HEADER = "x-recall-app";
// The iPhone app signs in through the system sign-in sheet, which comes back to the app's own
// address (RECALL_APP_REDIRECT, such as com.example.recall://signed-in). It is a setting, so the
// app's identifier is not in this repository; without it the app's sign-in is off.
const APP_ADDRESS = /^[a-z][a-z0-9+.-]*:\/\/[^\s?#]+$/i;
export const EXCHANGE_PATH = "/auth/app/exchange";

const AGENT = "recall-online"; // Descope refuses some default user agents
const SCOPE = "recall:read recall:write recall:forget offline_access";
const SESSION_MS = 30 * 24 * 60 * 60 * 1000; // a session unused this long is over
const LOGIN_MS = 10 * 60 * 1000; // how long a started sign-in may take
const EARLY_MS = 60 * 1000; // refresh a token this long before it expires
const TOUCH_MS = 60 * 60 * 1000; // record "last used" at most this often
const METADATA_MS = 60 * 60 * 1000;
const HANDOFF_MS = 2 * 60 * 1000; // how long the app has to collect its session
const CHALLENGE = /^[A-Za-z0-9_-]{43}$/; // base64url SHA-256, as PKCE sends it

// The authorization server could not answer just now (an error of its own, or too many
// requests). Not a refusal: the session it was asked about is still good.
export class Unavailable extends Error {}

function b64url(bytes) {
	let text = "";
	for (const byte of new Uint8Array(bytes)) text += String.fromCharCode(byte);
	return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function randomId(bytes = 32) {
	return b64url(crypto.getRandomValues(new Uint8Array(bytes)));
}

export async function challengeFor(verifier) {
	return b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
}

export function cookiesOf(request) {
	const found = {};
	for (const part of (request.headers.get("cookie") ?? "").split(";")) {
		const at = part.indexOf("=");
		if (at > 0) found[part.slice(0, at).trim()] = part.slice(at + 1).trim();
	}
	return found;
}

function cookie(name, value, maxAgeSeconds) {
	return `${name}=${value}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=${maxAgeSeconds}`;
}

// Where to go after signing in: only a path on this site, never another address.
export function safeNext(value) {
	if (typeof value !== "string" || !/^\/(?![/\\])/.test(value) || /[\\\r\n]/.test(value)) return "/";
	if (value.startsWith("/auth/") || value.startsWith("/api/")) return "/";
	return value.slice(0, 500);
}

function page(status, title, text) {
	const body = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>recall</title><body style="font:16px system-ui;max-width:28rem;margin:20vh auto;padding:0 1.5rem;color:#222;background:#fafafa">
<h1 style="font-size:1.25rem">${title}</h1><p>${text}</p><p><a href="/auth/login">Sign in again</a></p>`;
	return new Response(body, {
		status,
		headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" },
	});
}

function redirect(location, cookies = []) {
	const headers = new Headers({ location, "cache-control": "no-store" });
	for (const one of cookies) headers.append("set-cookie", one);
	return new Response(null, { status: 302, headers });
}

export class Sessions {
	metadata;
	refreshing = new Map();

	// store: get/put/delete/list as on Durable Object storage. issuer: the authorization
	// server. resource: recall's public MCP address, which is also the token audience.
	// allowed(token): resolves to the user id when recall accepts the token, else null.
	// appRedirect: where the iPhone app's sign-in returns to; empty turns that sign-in off.
	constructor({ store, issuer, resource, allowed, appRedirect = "", fetch = globalThis.fetch.bind(globalThis), now = Date.now }) {
		Object.assign(this, { store, issuer: String(issuer).replace(/\/+$/, ""), resource, allowed, fetch, now });
		const address = String(appRedirect ?? "").trim();
		if (address && !APP_ADDRESS.test(address)) console.warn("RECALL_APP_REDIRECT is not an address like com.example.recall://signed-in; the app's sign-in is off");
		this.appRedirect = APP_ADDRESS.test(address) ? address : "";
		this.origin = new URL(resource).origin;
	}

	get clientId() {
		return this.origin + CLIENT_PATH;
	}

	get redirectUri() {
		return this.origin + CALLBACK_PATH;
	}

	// The document the authorization server reads to learn who this client is.
	clientDocument() {
		return {
			client_id: this.clientId,
			client_name: "recall",
			client_uri: this.origin,
			redirect_uris: [this.redirectUri],
			grant_types: ["authorization_code", "refresh_token"],
			response_types: ["code"],
			token_endpoint_auth_method: "none",
			scope: SCOPE,
		};
	}

	async endpoints() {
		if (!this.metadata || this.now() - this.metadata.at > METADATA_MS) {
			const resp = await this.fetch(`${this.issuer}/.well-known/oauth-authorization-server`, {
				headers: { "user-agent": AGENT, accept: "application/json" },
			});
			if (!resp.ok) throw new Error(`the authorization server's settings could not be read (${resp.status})`);
			const found = await resp.json();
			for (const name of ["authorization_endpoint", "token_endpoint"]) {
				if (typeof found[name] !== "string" || !found[name].startsWith("https://")) {
					throw new Error(`the authorization server publishes no usable ${name}`);
				}
			}
			this.metadata = { at: this.now(), ...found };
		}
		return this.metadata;
	}

	// Answers /auth/* and the client document; null for any other path.
	async handle(request) {
		const url = new URL(request.url);
		if (url.pathname === CLIENT_PATH && request.method === "GET") {
			return Response.json(this.clientDocument(), { headers: { "cache-control": "public, max-age=3600" } });
		}
		if (url.pathname === "/auth/login" && request.method === "GET") return this.login(url);
		if (url.pathname === CALLBACK_PATH && request.method === "GET") return this.callback(request, url);
		if (url.pathname === "/auth/logout" && request.method === "POST") return this.logout(request);
		if (url.pathname === EXCHANGE_PATH && request.method === "POST") return this.exchange(request);
		if (url.pathname.startsWith("/auth/")) return new Response("not found", { status: 404 });
		return null;
	}

	// `?app=<challenge>` starts a sign-in for the iPhone app: the app keeps the verifier behind
	// that challenge, and only it can turn the one-time code it gets back into a session.
	async login(url) {
		const app = url.searchParams.get("app");
		if (app !== null && !CHALLENGE.test(app)) return page(400, "That sign-in could not start", "The app sent a sign-in request this site does not understand.");
		if (app !== null && !this.appRedirect) return page(404, "That sign-in is not set up", "This site has no address to return to the app (RECALL_APP_REDIRECT).");
		const { authorization_endpoint: authorize } = await this.endpoints();
		const state = randomId();
		const verifier = randomId(48);
		const started = { verifier, next: safeNext(url.searchParams.get("next")), at: this.now() };
		if (app !== null) started.app = app;
		await this.store.put(`login:${state}`, started);
		const to = new URL(authorize);
		for (const [name, value] of Object.entries({
			response_type: "code",
			client_id: this.clientId,
			redirect_uri: this.redirectUri,
			scope: SCOPE,
			state,
			code_challenge: await challengeFor(verifier),
			code_challenge_method: "S256",
			resource: this.resource,
		})) {
			to.searchParams.set(name, value);
		}
		// The cookie ties the sign-in to this browser: a callback opened anywhere else is refused.
		return redirect(to.toString(), [cookie(LOGIN_COOKIE, state, LOGIN_MS / 1000)]);
	}

	async callback(request, url) {
		const state = url.searchParams.get("state") ?? "";
		const started = state ? await this.store.get(`login:${state}`) : undefined;
		if (state) await this.store.delete(`login:${state}`);
		const clear = cookie(LOGIN_COOKIE, "", 0);
		const fail = (status, title, text) => {
			const resp = page(status, title, text);
			resp.headers.append("set-cookie", clear);
			return resp;
		};
		if (!started || cookiesOf(request)[LOGIN_COOKIE] !== state || this.now() - started.at > LOGIN_MS) {
			return fail(400, "That sign-in did not finish", "It took too long or was started in another browser.");
		}
		const code = url.searchParams.get("code");
		if (!code) return fail(400, "Sign-in was not completed", "The sign-in page returned without an approval.");
		const tokens = await this.token({
			grant_type: "authorization_code", code, redirect_uri: this.redirectUri, code_verifier: started.verifier,
		}).catch((error) => {
			if (error instanceof Unavailable) return null;
			throw error;
		});
		if (!tokens) return fail(502, "Sign-in could not be completed", "The sign-in service did not accept the request.");
		const user = await this.allowed(tokens.access);
		if (!user) return fail(403, "This account cannot use recall", "recall is private and this account is not on its list.");
		const id = randomId();
		await this.store.put(`session:${id}`, { ...tokens, user, created: this.now(), used: this.now() });
		if (started.app) {
			// The sheet hands the app a one-time code, never the session id itself.
			const handoff = randomId();
			await this.store.put(`handoff:${handoff}`, { session: id, challenge: started.app, at: this.now() });
			return redirect(`${this.appRedirect}?code=${handoff}`, [clear]);
		}
		return redirect(started.next, [cookie(COOKIE, id, SESSION_MS / 1000), clear]);
	}

	// The app's second step: its one-time code and the verifier behind its challenge, for the
	// session id. The app then sends that id as the session cookie, as the web app's browser does.
	async exchange(request) {
		const fail = (status, error) => Response.json({ error }, { status, headers: { "cache-control": "no-store" } });
		if (!request.headers.has(APP_HEADER) || request.headers.get("origin") !== null) return fail(403, "this request did not come from the recall app");
		let body;
		try {
			body = await request.json();
		} catch {
			return fail(400, "send JSON with code and verifier");
		}
		const { code, verifier } = body ?? {};
		if (typeof code !== "string" || !/^[A-Za-z0-9_-]{20,100}$/.test(code) || typeof verifier !== "string" || verifier.length < 43 || verifier.length > 128) {
			return fail(400, "send JSON with code and verifier");
		}
		const handoff = await this.store.get(`handoff:${code}`);
		if (handoff) await this.store.delete(`handoff:${code}`); // one try, right or wrong
		if (!handoff || this.now() - handoff.at > HANDOFF_MS || (await challengeFor(verifier)) !== handoff.challenge) {
			if (handoff) await this.store.delete(`session:${handoff.session}`);
			return fail(400, "that sign-in code is not valid; sign in again");
		}
		return Response.json({ session: handoff.session, cookie: COOKIE }, { headers: { "cache-control": "no-store" } });
	}

	async logout(request) {
		const id = cookiesOf(request)[COOKIE];
		if (id) {
			const session = await this.store.get(`session:${id}`);
			await this.store.delete(`session:${id}`);
			if (session?.refresh) await this.revoke(session.refresh);
		}
		return new Response(null, { status: 204, headers: { "set-cookie": cookie(COOKIE, "", 0), "cache-control": "no-store" } });
	}

	// One call to the token endpoint. Returns {access, refresh, expires} or null when refused.
	// Throws Unavailable when the server failed to answer (5xx, 408, 429) rather than said no.
	async token(fields) {
		const { token_endpoint: endpoint } = await this.endpoints();
		const resp = await this.fetch(endpoint, {
			method: "POST",
			headers: { "content-type": "application/x-www-form-urlencoded", accept: "application/json", "user-agent": AGENT },
			body: new URLSearchParams({ ...fields, client_id: this.clientId, resource: this.resource }).toString(),
		});
		if (!resp.ok) {
			console.warn("token request refused", resp.status, fields.grant_type);
			if (resp.status >= 500 || resp.status === 408 || resp.status === 429) {
				throw new Unavailable(`the sign-in service answered ${resp.status}`);
			}
			return null;
		}
		const got = await resp.json();
		if (typeof got.access_token !== "string") return null;
		const seconds = Number(got.expires_in) > 0 ? Number(got.expires_in) : 300;
		return { access: got.access_token, refresh: got.refresh_token ?? fields.refresh_token ?? null, expires: this.now() + seconds * 1000 };
	}

	async revoke(refresh) {
		try {
			const { revocation_endpoint: endpoint } = await this.endpoints();
			if (!endpoint) return;
			await this.fetch(endpoint, {
				method: "POST",
				headers: { "content-type": "application/x-www-form-urlencoded", "user-agent": AGENT },
				body: new URLSearchParams({ token: refresh, token_type_hint: "refresh_token", client_id: this.clientId }).toString(),
			});
		} catch (error) {
			console.warn("could not revoke a refresh token", error);
		}
	}

	// The signed-in session behind a request: {id, token, user, cookie?} or null.
	// `cookie` is set when the browser's cookie should be renewed with the answer.
	async access(request) {
		const id = cookiesOf(request)[COOKIE];
		if (!id || !/^[A-Za-z0-9_-]{20,100}$/.test(id)) return null;
		let session = await this.store.get(`session:${id}`);
		if (!session) return null;
		if (this.now() - session.used > SESSION_MS) {
			await this.store.delete(`session:${id}`);
			return null;
		}
		if (session.expires - this.now() < EARLY_MS) {
			try {
				session = await this.refresh(id);
			} catch (error) {
				// The early refresh could not be made; the token in hand still has a little time.
				if (!(error instanceof Unavailable) || session.expires <= this.now()) throw error;
			}
			if (!session) return null;
		}
		let renew;
		if (this.now() - session.used > TOUCH_MS) {
			session = { ...session, used: this.now() };
			await this.store.put(`session:${id}`, session);
			renew = cookie(COOKIE, id, SESSION_MS / 1000);
		}
		return { id, token: session.access, user: session.user, cookie: renew };
	}

	// A new access token for a session. Requests that arrive together share one refresh,
	// because the authorization server may accept each refresh token only once. A refusal ends
	// the session; when the server only failed to answer (Unavailable) the session is kept.
	refresh(id) {
		if (!this.refreshing.has(id)) {
			this.refreshing.set(id, this.refreshOnce(id).finally(() => this.refreshing.delete(id)));
		}
		return this.refreshing.get(id);
	}

	async refreshOnce(id) {
		const session = await this.store.get(`session:${id}`);
		if (!session?.refresh) {
			await this.store.delete(`session:${id}`);
			return null;
		}
		const tokens = await this.token({ grant_type: "refresh_token", refresh_token: session.refresh });
		if (!tokens) {
			await this.store.delete(`session:${id}`);
			return null;
		}
		const next = { ...session, ...tokens };
		await this.store.put(`session:${id}`, next);
		return next;
	}

	// A request that changes something must come from the app's own pages: browsers send the
	// cookie along with some requests other sites start, and those must not count.
	fromApp(request) {
		if (request.method === "GET" || request.method === "HEAD") return true;
		const origin = request.headers.get("origin");
		return request.headers.has(APP_HEADER) && (origin === null || origin === this.origin);
	}

	// Drop sessions nobody used for a month and sign-ins that were never finished.
	async sweep() {
		let removed = 0;
		for (const [key, value] of await this.store.list({ prefix: "session:" })) {
			if (this.now() - (value?.used ?? 0) > SESSION_MS) removed += Number(await this.store.delete(key));
		}
		for (const [key, value] of await this.store.list({ prefix: "login:" })) {
			if (this.now() - (value?.at ?? 0) > LOGIN_MS) removed += Number(await this.store.delete(key));
		}
		for (const [key, value] of await this.store.list({ prefix: "handoff:" })) {
			if (this.now() - (value?.at ?? 0) > HANDOFF_MS) {
				removed += Number(await this.store.delete(key));
				await this.store.delete(`session:${value?.session}`); // never collected
			}
		}
		return removed;
	}
}
