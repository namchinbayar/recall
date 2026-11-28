// Where browsers send what the app's Content-Security-Policy blocked (see app/_headers).
//
// The reports are for one purpose: noticing that a change to the app or its headers made a
// browser refuse something. So each one is cut down to what that takes and written to the
// Worker's log as one line. Nothing else is kept. A report names the page it came from, and a
// page address can hold a search someone typed, so addresses lose their query and fragment
// here; the sample of a Trusted Types violation starts with the text that was written into
// the page, so only the name of the place it was written to is kept.
//
// Anyone can post to this address (a browser sends reports without a sign-in), so the body is
// small, the number of reports per request is small, and the answer never says what was wrong.

export const REPORTS_PATH = "/reports";
const MAX_BYTES = 16_000;
const MAX_REPORTS = 10;

/** An address without its query and fragment; a keyword such as "inline" or "eval" as it is. */
export function bare(value) {
	const text = typeof value === "string" ? value.slice(0, 400) : "";
	if (!text) return "";
	try {
		const url = new URL(text);
		return url.protocol === "http:" || url.protocol === "https:" ? url.origin + url.pathname : `${url.protocol}`;
	} catch {
		return text.slice(0, 60); // not an address: 'inline', 'eval', 'trusted-types-sink', 'self' ...
	}
}

/** What is worth logging of one report, in either format browsers send. */
export function summary(report) {
	const body = report?.body ?? report?.["csp-report"] ?? {};
	const directive = String(body.effectiveDirective ?? body["effective-directive"] ?? body["violated-directive"] ?? "").slice(0, 60);
	const blocked = bare(body.blockedURL ?? body["blocked-uri"]);
	let sample = String(body.sample ?? body["script-sample"] ?? "");
	// "Element innerHTML|<p>the text that was written…": keep the sink, drop the text.
	sample = directive.startsWith("require-trusted-types-for") || blocked === "trusted-types-sink" ? sample.split("|")[0] : sample;
	return {
		type: String(report?.type ?? (report?.["csp-report"] ? "csp-violation" : "")).slice(0, 40),
		directive,
		blocked,
		page: bare(body.documentURL ?? body["document-uri"] ?? report?.url),
		source: bare(body.sourceFile ?? body["source-file"]),
		line: Number.isInteger(body.lineNumber ?? body["line-number"]) ? (body.lineNumber ?? body["line-number"]) : undefined,
		sample: sample.slice(0, 60),
		mode: String(body.disposition ?? "").slice(0, 10),
	};
}

/** Take a browser's report request, log each report as one line, and say nothing back. */
export async function receive(request, log = console.log) {
	if (request.method !== "POST") return new Response(null, { status: 405, headers: { allow: "POST" } });
	const done = new Response(null, { status: 204, headers: { "cache-control": "no-store" } });
	const text = (await request.text()).slice(0, MAX_BYTES);
	let data;
	try {
		data = JSON.parse(text);
	} catch {
		return done;
	}
	const reports = Array.isArray(data) ? data : [data];
	for (const report of reports.slice(0, MAX_REPORTS)) {
		if (report && typeof report === "object") log(`browser-report ${JSON.stringify(summary(report))}`);
	}
	return done;
}
