import assert from "node:assert/strict";
import { test } from "node:test";
import { ardManifest, assess, isPreview, medianRun } from "./lighthouse-checks.mjs";

// A result shaped as Lighthouse writes one, every target met unless a test changes it.
function lhr(url, { scores = {}, audits = {} } = {}) {
  const audit = (id, score, mode = "binary") => ({ id, title: id, score, scoreDisplayMode: mode });
  const all = {
    lcp: audit("lcp", 0.95, "numeric"),
    "color-contrast": audit("color-contrast", 1),
    "errors-in-console": audit("errors-in-console", 1),
    "is-crawlable": audit("is-crawlable", 1),
    "link-text": audit("link-text", 1),
    "ard-schema": audit("ard-schema", 1),
    "webmcp-form-coverage": audit("webmcp-form-coverage", null, "notApplicable"),
    ...audits,
  };
  const cat = (id, refs, score = 1) => ({ id, score: scores[id] ?? score, auditRefs: refs.map((r) => ({ id: r, weight: 1 })) });
  return {
    requestedUrl: url,
    finalDisplayedUrl: url,
    audits: all,
    categories: {
      performance: cat("performance", ["lcp"], 0.97),
      accessibility: cat("accessibility", ["color-contrast"]),
      "best-practices": cat("best-practices", ["errors-in-console"]),
      seo: cat("seo", ["is-crawlable", "link-text"]),
      "agentic-browsing": cat("agentic-browsing", ["ard-schema", "webmcp-form-coverage"]),
    },
  };
}
const PROD = "https://publicdata.au/";
const PREVIEW = "https://pr-84.publicdata-au.pages.dev/";
const failed = (r) => r.failures.map((f) => f.audit || f.category).sort();

test("a result that meets every target passes, with a not-applicable audit ignored", () => {
  assert.deepEqual(assess(lhr(PROD)).failures, []);
});

test("performance under 90 fails and names its weak audits", () => {
  const r = assess(lhr(PROD, { scores: { performance: 0.89 }, audits: { lcp: { id: "lcp", title: "LCP", score: 0.5, scoreDisplayMode: "numeric", displayValue: "4.1 s" } } }));
  assert.deepEqual(failed(r), ["lcp", "performance"]);
});

test("one agentic audit short of a pass fails, though the category rounds to 98", () => {
  const r = assess(lhr(PROD, { scores: { "agentic-browsing": 0.98 }, audits: { "ard-schema": { id: "ard-schema", title: "ARD", score: 0.9, scoreDisplayMode: "numeric" } } }));
  assert.deepEqual(failed(r), ["ard-schema"]);
});

test("a category missing from the result fails", () => {
  const x = lhr(PROD);
  delete x.categories["agentic-browsing"];
  assert.deepEqual(failed(assess(x)), ["agentic-browsing"]);
});

test("an audit that errored fails rather than passing unseen", () => {
  const r = assess(lhr(PROD, { audits: { "link-text": { id: "link-text", title: "t", score: null, scoreDisplayMode: "error", errorMessage: "boom" } } }));
  assert.deepEqual(failed(r), ["link-text"]);
});

test("is-crawlable is skipped on a preview host only", () => {
  const noindex = { audits: { "is-crawlable": { id: "is-crawlable", title: "crawlable", score: 0, scoreDisplayMode: "binary" } } };
  const pre = assess(lhr(PREVIEW, noindex));
  assert.deepEqual(pre.failures, []);
  assert.deepEqual(pre.skipped, ["is-crawlable"]);
  assert.deepEqual(failed(assess(lhr(PROD, noindex))), ["is-crawlable"]);
  const linkText = { audits: { ...noindex.audits, "link-text": { id: "link-text", title: "t", score: 0, scoreDisplayMode: "binary" } } };
  assert.deepEqual(failed(assess(lhr(PREVIEW, linkText))), ["link-text"]);
});

test("a preview is a pages.dev host", () => {
  assert.equal(isPreview(PREVIEW), true);
  assert.equal(isPreview(PROD), false);
});

test("the median run is the middle performance score", () => {
  const runs = [0.91, 0.99, 0.95].map((s) => lhr(PROD, { scores: { performance: s } }));
  assert.equal(medianRun(runs).categories.performance.score, 0.95);
});

test("is-crawlable is skipped on production only for a page named noindex on purpose", () => {
  const noindex = { audits: { "is-crawlable": { id: "is-crawlable", title: "crawlable", score: 0, scoreDisplayMode: "binary" } } };
  assert.deepEqual(assess(lhr(`${PROD}d/qld-road-crash-locations/explore/`, noindex)).failures, []);
  assert.deepEqual(failed(assess(lhr(`${PROD}d/qld-road-crash-locations/`, noindex))), ["is-crawlable"]);
});

test("the ARD manifest is found in the gatherer's order and resolved against the page", () => {
  const page = `${PREVIEW}d/x/`;
  assert.equal(ardManifest(page), `${PREVIEW}.well-known/ai-catalog.json`);
  assert.equal(ardManifest(page, { linkHeader: '</cat.json>; rel="ai-catalog"' }), `${PREVIEW}cat.json`);
  const html = '<head><link rel="ai-catalog" href="https://publicdata.au/.well-known/ai-catalog.json"></head>';
  assert.equal(ardManifest(page, { html, linkHeader: '</cat.json>; rel="ai-catalog"' }), "https://publicdata.au/.well-known/ai-catalog.json");
  assert.equal(ardManifest(page, { robotsTxt: "User-Agent: *\nAgentmap: /.well-known/ai-catalog.json\n", html }), `${PREVIEW}.well-known/ai-catalog.json`);
});

test("an audit named in skip is not held", () => {
  const bad = { audits: { "ard-schema": { id: "ard-schema", title: "ARD", score: 0.9, scoreDisplayMode: "numeric" } } };
  assert.deepEqual(assess(lhr(PREVIEW, bad), { skip: ["ard-schema"] }).failures, []);
  assert.deepEqual(failed(assess(lhr(PREVIEW, bad))), ["ard-schema"]);
});
