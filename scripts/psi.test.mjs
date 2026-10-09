import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const SCRIPT = fileURLToPath(new URL("./psi.mjs", import.meta.url));

// Stands in for the PageSpeed Insights API. PSI_STUB names what it answers: an HTTP status for
// every call, "flaky", where each page misses performance on its first call and passes after,
// "slow", where every call misses it, "nofcp", where Lighthouse cannot load any page, "mixed",
// where / misses on every call and /backlog/ is refused, or "miss-then-error", where / misses on
// its first call and the second is refused. Retry waits are cut to nothing.
const STUB = `
const mode = process.env.PSI_STUB;
const seen = new Map();
const cat = (score) => ({ score, auditRefs: [] });
const wait = globalThis.setTimeout;
globalThis.setTimeout = (f, ms, ...a) => wait(f, 0, ...a);
const refuse = (status) => new Response(JSON.stringify({ error: { message: "API key not valid" } }), { status });
globalThis.fetch = async (u) => {
  const q = new URL(u).searchParams;
  if (/^\\d+$/.test(mode)) return refuse(Number(mode));
  if (mode === "nofcp") return new Response(JSON.stringify({ error: { message: "Lighthouse returned error: NO_FCP. The page did not paint any content. (NO_FCP)" } }), { status: 500 });
  const path = new URL(q.get("url")).pathname;
  const k = q.get("url") + q.get("strategy");
  const n = (seen.get(k) || 0) + 1;
  seen.set(k, n);
  if (mode === "mixed" && path === "/backlog/") return refuse(400);
  if (mode === "miss-then-error" && path === "/" && n === 2) return refuse(400);
  const score = mode === "slow" || (mode === "flaky" ? n === 1 : path === "/") ? 0.5 : 0.95;
  const categories = Object.fromEntries(q.getAll("category").map((c) => [c, cat(1)]));
  categories.performance = cat(score);
  return Response.json({ lighthouseResult: { lighthouseVersion: "13.5.0", requestedUrl: q.get("url"), audits: {}, categories } });
};
`;

function run(stub) {
  const cwd = mkdtempSync(join(tmpdir(), "psi-"));
  const r = spawnSync(process.execPath, ["--import", `data:text/javascript,${encodeURIComponent(STUB)}`, SCRIPT, "https://publicdata.au"], {
    cwd,
    env: { ...process.env, PSI_API_KEY: "test-key", PSI_STUB: stub },
    encoding: "utf8",
  });
  return { code: r.status, out: r.stdout + r.stderr, report: readFileSync(join(cwd, "psi-report.md"), "utf8") };
}

for (const status of [400, 403]) {
  test(`an API refusal (HTTP ${status}) exits 3, so no missed-targets issue is opened`, () => {
    const r = run(String(status));
    assert.equal(r.code, 3, r.out);
    assert.match(r.report, /## The API calls that failed/);
    assert.doesNotMatch(r.report, /## What failed/);
    assert.doesNotMatch(r.out + r.report, /test-key/);
  });
}

test("a page that misses a target once and passes on the second run passes", () => {
  const r = run("flaky");
  assert.equal(r.code, 0, r.out);
  assert.match(r.report, /Every page met every target\./);
});

test("a page that misses a target on both runs exits 1 and names it", () => {
  const r = run("slow");
  assert.equal(r.code, 1, r.out);
  assert.match(r.report, /## What failed\n\n- \/ \(mobile\) performance: score 50, needs 90/);
});

test("a page that misses on both runs exits 1 although another page's call failed, and the report names both", () => {
  const r = run("mixed");
  assert.equal(r.code, 1, r.out);
  assert.match(r.report, /## What failed\n\n- \/ \(mobile\) performance: score 50, needs 90/);
  assert.match(r.report, /## The API calls that failed\n\n- \/backlog\/ \(mobile\): mobile https:\/\/publicdata\.au\/backlog\/: HTTP 400/);
});

test("a page that misses and whose second run the API refuses keeps its miss", () => {
  const r = run("miss-then-error");
  assert.equal(r.code, 1, r.out);
  assert.match(r.report, /## What failed\n\n- \/ \(mobile\) performance: score 50, needs 90/);
  assert.match(r.report, /- \/ \(mobile\), second run: mobile https:\/\/publicdata\.au\/: HTTP 400/);
});

test("a page Lighthouse cannot load (NO_FCP) is a miss, with no hint about the key", () => {
  const r = run("nofcp");
  assert.equal(r.code, 1, r.out);
  assert.match(r.report, /## What failed\n\n- \/ \(mobile\) load: Lighthouse returned error: NO_FCP/);
  assert.doesNotMatch(r.report, /## The API calls that failed/);
  assert.doesNotMatch(r.out, /PSI_API_KEY/);
});
