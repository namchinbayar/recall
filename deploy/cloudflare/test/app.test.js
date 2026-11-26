// The app is static files with no build step, so nothing else notices a missing file, a path
// the Worker no longer receives, or page code that starts writing HTML. These checks do.
import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { test } from "node:test";
import { CALLBACK_PATH, CLIENT_PATH } from "../src/session.js";

const here = new URL("..", import.meta.url);
const read = (path) => readFileSync(new URL(path, here), "utf8");
const inApp = (path) => existsSync(new URL(`app${path === "/" ? "/index.html" : path}`, here));
const config = JSON.parse(read("wrangler.jsonc").replace(/^\s*\/\/.*$/gm, ""));
const workerFirst = config.assets.run_worker_first;
const toWorker = (path) => workerFirst.some((rule) => (rule.endsWith("/*") ? path.startsWith(rule.slice(0, -1)) : path === rule));
const list = (source, name) => JSON.parse(new RegExp(`const ${name} = (\\[[^\\]]*\\])`).exec(source)[1].replace(/,\s*\]/, "]"));

test("the page only refers to files that exist", () => {
	const paths = [...read("app/index.html").matchAll(/(?:href|src)="(\/[^"#]*)"/g)].map((m) => m[1]);
	assert.ok(paths.includes("/app.js") && paths.includes("/app.css") && paths.includes("/manifest.webmanifest"));
	for (const path of paths) assert.ok(inApp(path) || path in { "/search": 1, "/memories": 1, "/timeline": 1 }, `${path} is missing`);
	for (const [, font] of read("app/app.css").matchAll(/url\("([^"]+)"\)/g)) assert.ok(inApp(font), `${font} is missing`);
});

test("the manifest can be installed: a fixed id and icons that exist", () => {
	const manifest = JSON.parse(read("app/manifest.webmanifest"));
	assert.equal(manifest.id, "/");
	assert.equal(manifest.display, "standalone");
	assert.ok(manifest.icons.some((icon) => icon.sizes === "192x192") && manifest.icons.some((icon) => icon.sizes === "512x512"));
	for (const icon of manifest.icons) assert.ok(inApp(icon.src), `${icon.src} is missing`);
});

test("the offline worker keeps only the app's own files, and all of them exist", () => {
	const sw = read("app/sw.js");
	for (const path of list(sw, "FILES")) assert.ok(inApp(path), `${path} is missing`);
	assert.ok(list(sw, "FILES").includes("/app.js"));
});

test("the API, the sign-in and the MCP endpoint reach the Worker and are never stored offline", () => {
	const skipped = list(read("app/sw.js"), "NOT_OURS");
	for (const path of ["/mcp", "/mcp/", "/api/v1/status", "/auth/login", CALLBACK_PATH, CLIENT_PATH, "/healthz",
		"/.well-known/oauth-protected-resource/mcp"]) {
		assert.ok(toWorker(path), `${path} would be answered by a static file`);
		assert.ok(skipped.some((prefix) => path.startsWith(prefix)), `${path} would go through the offline worker`);
	}
	for (const page of ["/", "/search", "/item", "/app.js"]) assert.ok(!toWorker(page), `${page} should not need the Worker`);
	assert.equal(config.assets.not_found_handling, "single-page-application");
});

test("the app directory holds nothing unexpected, and the pages never build HTML from text", () => {
	const files = readdirSync(new URL("app", here)).sort();
	assert.deepEqual(files, ["_headers", "app.css", "app.js", "fonts", "icons", "index.html", "manifest.webmanifest", "sw.js"]);
	assert.doesNotMatch(read("app/app.js"), /innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(/);
	const headers = read("app/_headers");
	assert.match(headers, /Content-Security-Policy: default-src 'none'; script-src 'self';/);
	assert.doesNotMatch(headers, /unsafe-inline|unsafe-eval/);
	assert.doesNotMatch(read("app/index.html"), /<script(?![^>]*\bsrc=)|\son\w+=|style="/);
});
