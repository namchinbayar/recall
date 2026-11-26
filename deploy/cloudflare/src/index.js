// The Worker in front of recall's online server (the MCP endpoint and the app's web API).
//
// Every request goes to one Durable Object, which runs one container (the image built from
// ../Dockerfile). MCP requests are forwarded unchanged: recall itself checks the sign-in and
// this Worker adds no access of its own. For the recall app the Worker also does the sign-in
// (session.js): it keeps the app's tokens, and adds one to each /api request that arrives
// with the app's session cookie. recall checks that token like any other. A timer keeps the
// container running so the first request after a quiet spell does not wait for a start.
import { DurableObject } from "cloudflare:workers";
import { COOKIE, Sessions, Unavailable } from "./session.js";

const PORT = 8791;
const INSTANCE = "basic";
const IDLE_MS = 6 * 60 * 60 * 1000; // the longest inactivity timeout Cloudflare allows
const KEEPALIVE_MS = 5 * 60 * 1000;
const START_TRIES = 90; // about 90 seconds for the first start
const SWEEP_MS = 60 * 60 * 1000; // how often ended sessions are cleared out
const MAX_BODY = 400_000; // bytes of one app request, the same limit recall applies

const json = (body, status, headers = {}) =>
	Response.json(body, { status, headers: { "cache-control": "no-store", ...headers } });

// Worker settings and secrets handed to the container as environment variables.
const SETTINGS = [
	"RECALL_ONLINE_URL", "RECALL_ONLINE_ISSUER", "RECALL_ONLINE_SUBJECTS",
	"MONGODB_URI", "MONGODB_USERNAME", "MONGODB_PASSWORD",
	"VOYAGE_API_KEY", "VOYAGE_MODEL", "VOYAGE_BASE_URL", "RECALL_DB",
	"RECALL_VAULT_SECRET", "RECALL_AGENT_KEYS", "RECALL_OWNER",
];

export class RecallServer extends DurableObject {
	starting;
	sessions;
	swept = 0;

	constructor(ctx, env) {
		super(ctx, env);
		if (env.RECALL_ONLINE_ISSUER && env.RECALL_ONLINE_URL) {
			this.sessions = new Sessions({
				store: ctx.storage,
				issuer: env.RECALL_ONLINE_ISSUER,
				resource: env.RECALL_ONLINE_URL,
				allowed: (token) => this.userOf(token),
			});
		}
		if (ctx.container.running) {
			// The timeout does not survive a restart of this object; set it again.
			void ctx.blockConcurrencyWhile(() => ctx.container.setInactivityTimeout(IDLE_MS));
		}
	}

	async fetch(request) {
		if (this.sessions) {
			const answer = await this.sessions.handle(request); // the sign-in pages
			if (answer) return answer;
			// The app's requests carry a cookie. A request with its own token is forwarded as it is.
			const path = new URL(request.url).pathname;
			if (path.startsWith("/api/") && !request.headers.has("authorization")) return this.api(request);
		}
		await this.ready();
		return this.send(request);
	}

	// A request from the app: find its session and forward it with that session's token.
	async api(request) {
		try {
			return await this.forApp(request);
		} catch (error) {
			if (!(error instanceof Unavailable)) throw error;
			// The session is kept, so the app only has to ask again.
			return json({ error: "the sign-in service is not answering; try again in a moment" }, 503, { "retry-after": "10" });
		}
	}

	async forApp(request) {
		if (!this.sessions.fromApp(request)) return json({ error: "this request did not come from the recall app" }, 403);
		const session = await this.sessions.access(request);
		if (!session) return json({ error: "sign in first" }, 401);
		const body = request.method === "GET" || request.method === "HEAD" ? undefined : await request.arrayBuffer();
		if (body && body.byteLength > MAX_BODY) return json({ error: "the request is too large" }, 413);
		await this.ready();
		let answer = await this.send(request, session.token, body);
		if (answer.status === 401) {
			// recall no longer accepts the token (it can expire early): get a new one and try once more.
			const renewed = await this.sessions.refresh(session.id);
			if (!renewed) return json({ error: "sign in first" }, 401, { "set-cookie": `${COOKIE}=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0` });
			answer = await this.send(request, renewed.access, body);
		}
		const out = new Response(answer.body, answer);
		out.headers.delete("www-authenticate");
		if (session.cookie) out.headers.append("set-cookie", session.cookie);
		return out;
	}

	// Forward a request to the container: unchanged, or (for the app) with a session's token
	// in place of the browser's cookie.
	send(request, token, body) {
		const url = new URL(request.url);
		url.protocol = "http:";
		url.host = "container";
		if (token === undefined) {
			const forwarded = new Request(url, request);
			forwarded.headers.delete("host");
			return this.ctx.container.getTcpPort(PORT).fetch(forwarded);
		}
		const headers = new Headers(request.headers);
		for (const name of ["host", "cookie", "content-length"]) headers.delete(name);
		headers.set("authorization", `Bearer ${token}`);
		return this.ctx.container.getTcpPort(PORT).fetch(new Request(url, { method: request.method, headers, body }));
	}

	// Whom recall takes a token to be from: the user id, or null when recall refuses it.
	async userOf(token) {
		await this.ready();
		const answer = await this.ctx.container
			.getTcpPort(PORT)
			.fetch("http://container/api/v1/me", { headers: { authorization: `Bearer ${token}` } });
		if (!answer.ok) return null;
		const { user } = await answer.json();
		return typeof user === "string" && user ? user : null;
	}

	// Runs every few minutes: starts the container if it stopped, and counts as activity.
	async alarm() {
		try {
			await this.ready();
		} catch (error) {
			console.error("keep-alive: the container did not start", error);
		}
		if (this.sessions && Date.now() - this.swept > SWEEP_MS) {
			this.swept = Date.now();
			await this.sessions.sweep().catch((error) => console.error("could not clear ended sessions", error));
		}
		await this.ctx.storage.setAlarm(Date.now() + KEEPALIVE_MS);
	}

	// Concurrent requests share one start; a failed start is retried by the next request.
	ready() {
		this.starting ??= this.startAndWait().finally(() => {
			this.starting = undefined;
		});
		return this.starting;
	}

	async startAndWait() {
		const container = this.ctx.container;
		// A running container keeps the image and settings it started with. deploy.sh sets RELEASE
		// to the deployed commit; a new value replaces the container, which is how a new image or
		// secret takes effect.
		const release = String(this.env.RELEASE ?? "");
		if (container.running && (await this.ctx.storage.get("release")) !== release) {
			await container.destroy("new release");
			for (let i = 0; i < 30 && container.running; i++) {
				await new Promise((resolve) => setTimeout(resolve, 500));
			}
		}
		if (!container.running) {
			const env = { RECALL_ONLINE_HOSTS: "container" };
			for (const name of SETTINGS) {
				if (this.env[name]) env[name] = String(this.env[name]);
			}
			container.start({
				image: container.images.base,
				instance: INSTANCE,
				enableInternet: true, // the database and the embedding service are outside Cloudflare
				env,
			});
			await this.ctx.storage.put("release", release);
		}
		await container.setInactivityTimeout(IDLE_MS);
		if ((await this.ctx.storage.getAlarm()) === null) {
			await this.ctx.storage.setAlarm(Date.now() + KEEPALIVE_MS);
		}
		// Any answer from the health route means the server is listening.
		for (let i = 0; i < START_TRIES; i++) {
			try {
				await container.getTcpPort(PORT).fetch("http://container/healthz");
				return;
			} catch {
				await new Promise((resolve) => setTimeout(resolve, 1000));
			}
		}
		throw new Error("the recall container did not start listening in time");
	}
}

export default {
	fetch(request, env) {
		return env.RECALL.getByName("main").fetch(request);
	},
};
