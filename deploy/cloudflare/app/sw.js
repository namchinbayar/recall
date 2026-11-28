// Keeps the app's own files (pages, styles, fonts, icons) on the device, so recall opens when
// the connection is slow or gone. It never stores what recall returns: requests to the API,
// the sign-in pages and the MCP endpoint go straight to the network, untouched.
const CACHE = "recall-app-4";
const FILES = [
	"/", "/app.css", "/app.js", "/manifest.webmanifest",
	"/fonts/google-sans-flex.woff2", "/fonts/young-serif.woff2", "/fonts/google-sans-code.woff2",
	"/icons/icon.svg", "/icons/icon-192.png",
];
const NOT_OURS = ["/api/", "/auth/", "/oauth/", "/mcp", "/.well-known/", "/healthz", "/reports"];
const SLOW_MS = 4000;

self.addEventListener("install", (event) => {
	event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
	event.waitUntil(
		caches.keys()
			.then((names) => Promise.all(names.filter((name) => name !== CACHE).map((name) => caches.delete(name))))
			.then(() => self.clients.claim()),
	);
});

self.addEventListener("fetch", (event) => {
	const request = event.request;
	const url = new URL(request.url);
	if (request.method !== "GET" || url.origin !== self.location.origin) return;
	if (NOT_OURS.some((prefix) => url.pathname.startsWith(prefix))) return;
	// Every page of the app is the same file.
	const key = request.mode === "navigate" ? "/" : url.pathname;
	event.respondWith(fresh(request, key));
});

// The network first, so a new version shows up at once; the stored copy when that fails or stalls.
async function fresh(request, key) {
	const cache = await caches.open(CACHE);
	const stored = await cache.match(key);
	const network = fetch(request).then((answer) => {
		if (answer.ok && answer.type === "basic" && !answer.redirected) cache.put(key, answer.clone()).catch(() => {});
		return answer;
	});
	if (!stored) return network;
	const slow = new Promise((resolve) => setTimeout(resolve, SLOW_MS, stored));
	return Promise.race([network.catch(() => stored), slow]);
}
