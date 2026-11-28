// What a browser reports is cut down before it is logged: no query strings, no page text.
import assert from "node:assert/strict";
import { test } from "node:test";
import { REPORTS_PATH, bare, receive, summary } from "../src/reports.js";

const post = (body, type = "application/reports+json") =>
	new Request(`https://recall.example${REPORTS_PATH}`, { method: "POST", headers: { "content-type": type }, body });

test("an address loses its query and fragment; a keyword stays", () => {
	assert.equal(bare("https://recall.example/search?q=my+passport+number#top"), "https://recall.example/search");
	assert.equal(bare("inline"), "inline");
	assert.equal(bare("trusted-types-sink"), "trusted-types-sink");
	assert.equal(bare("data:text/html,<script>secret()</script>"), "data:");
	assert.equal(bare(undefined), "");
});

test("a report in the newer format is cut down to what explains it", () => {
	const line = summary({
		type: "csp-violation", url: "https://recall.example/item?id=note:abc",
		body: {
			documentURL: "https://recall.example/item?id=note:abc", effectiveDirective: "script-src-elem",
			blockedURL: "https://evil.example/x.js?token=1", sourceFile: "https://recall.example/app.js", lineNumber: 12,
			sample: "alert(document.cookie)", disposition: "enforce",
		},
	});
	assert.deepEqual(line, {
		type: "csp-violation", directive: "script-src-elem", blocked: "https://evil.example/x.js",
		page: "https://recall.example/item", source: "https://recall.example/app.js", line: 12,
		sample: "alert(document.cookie)", mode: "enforce",
	});
});

test("a Trusted Types report keeps the sink and drops the text that was written", () => {
	const line = summary({
		type: "csp-violation",
		body: { effectiveDirective: "require-trusted-types-for", blockedURL: "trusted-types-sink", sample: "Element innerHTML|<p>My passport number is…" },
	});
	assert.equal(line.sample, "Element innerHTML");
	assert.doesNotMatch(JSON.stringify(line), /passport/);
});

test("the older format is read too", () => {
	const line = summary({ "csp-report": { "document-uri": "https://recall.example/search?q=x", "violated-directive": "style-src-attr", "blocked-uri": "inline" } });
	assert.equal(line.directive, "style-src-attr");
	assert.equal(line.blocked, "inline");
	assert.equal(line.page, "https://recall.example/search");
});

test("receive logs one line per report, at most ten, and always answers 204", async () => {
	const lines = [];
	const many = JSON.stringify(Array.from({ length: 25 }, (_, n) => ({ type: "csp-violation", body: { effectiveDirective: `d${n}` } })));
	const answer = await receive(post(many), (line) => lines.push(line));
	assert.equal(answer.status, 204);
	assert.equal(lines.length, 10);
	assert.ok(lines[0].startsWith('browser-report {"type":"csp-violation","directive":"d0"'));

	assert.equal((await receive(post("not json"), (line) => lines.push(line))).status, 204);
	assert.equal((await receive(post('"just a string"'), (line) => lines.push(line))).status, 204);
	assert.equal(lines.length, 10);
	const get = await receive(new Request(`https://recall.example${REPORTS_PATH}`), (line) => lines.push(line));
	assert.equal(get.status, 405);
});
