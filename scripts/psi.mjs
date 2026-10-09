// Runs PageSpeed Insights over production, mobile and desktop, and holds each result to the same
// targets as the pull request job (lighthouse-checks.mjs). PSI runs the Lighthouse Google ships,
// which can be newer than the version the pull request job pins, so this is where a new or changed
// audit shows first. It also reports the Chrome UX Report field data. Usage: node scripts/psi.mjs
// [base-url]; the key is read from PSI_API_KEY. Writes psi-report.md and exits 1 when a page misses
// a target or Lighthouse could not load it. A failed API call, such as a bad key or a spent quota,
// is listed in the report, and when no page missed it exits 3, since the run has no result.
import { writeFile } from "node:fs/promises";
import { NOINDEX, PAGES, TARGETS, assess } from "./lighthouse-checks.mjs";

const base = process.argv[2] || "https://publicdata.au";
const key = process.env.PSI_API_KEY;
if (!key) {
  console.error("::error::PSI_API_KEY is not set. Add a PageSpeed Insights API key as the secret PSI_API_KEY in the psi environment.");
  process.exit(2);
}
const FIELD = {
  LARGEST_CONTENTFUL_PAINT_MS: ["LCP", (v) => `${(v / 1000).toFixed(1)} s`],
  INTERACTION_TO_NEXT_PAINT: ["INP", (v) => `${v} ms`],
  CUMULATIVE_LAYOUT_SHIFT_SCORE: ["CLS", (v) => (v / 100).toFixed(2)],
  FIRST_CONTENTFUL_PAINT_MS: ["FCP", (v) => `${(v / 1000).toFixed(1)} s`],
  EXPERIMENTAL_TIME_TO_FIRST_BYTE: ["TTFB", (v) => `${(v / 1000).toFixed(1)} s`],
};

// PSI answers with this when Lighthouse ran but the page itself failed, as with NO_FCP or
// FAILED_DOCUMENT_REQUEST. That is a result about the page, so it counts as a miss.
class PageError extends Error {}
const LIGHTHOUSE_ERROR = /^Lighthouse returned error: /;

// The key is never logged: errors report the page and status, not the request URL.
async function psi(url, strategy) {
  const q = new URLSearchParams({ url, strategy, key });
  for (const c of Object.keys(TARGETS)) q.append("category", c);
  let last;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const res = await fetch(`https://www.googleapis.com/pagespeedonline/v5/runPagespeed?${q}`, { signal: AbortSignal.timeout(180_000) });
      const body = await res.json();
      if (res.ok) return body;
      const message = body.error?.message || "no message";
      if (LIGHTHOUSE_ERROR.test(message)) throw new PageError(message);
      last = `HTTP ${res.status}: ${message}`;
      if (res.status < 500 && res.status !== 429) break;
    } catch (e) {
      if (e instanceof PageError) throw e;
      last = e.name === "TimeoutError" ? "timed out" : e.message;
    }
    if (attempt < 3) await new Promise((r) => setTimeout(r, 10_000 * attempt));
  }
  throw new Error(`${strategy} ${url}: ${last}`);
}

const field = (exp) => {
  if (!exp?.metrics) return "no field data";
  const parts = Object.entries(FIELD).filter(([k]) => exp.metrics[k]).map(([k, [name, f]]) => `${name} ${f(exp.metrics[k].percentile)} (${exp.metrics[k].category.toLowerCase()})`);
  return `${exp.overall_category ? `${exp.overall_category.toLowerCase()}: ` : ""}${parts.join(", ")}`;
};
const pct = (v) => (v === null || v === undefined ? "none" : Math.round(v * 100));

const rows = [];
const problems = [];
const apiErrors = [];
let origin = null;
let version = null;
for (const path of PAGES) {
  const url = new URL(path, base).href;
  for (const strategy of ["mobile", "desktop"]) {
    const opts = { preview: false, noindex: NOINDEX.has(path) };
    const sample = async () => {
      try {
        const r = await psi(url, strategy);
        return { r, ...assess(r.lighthouseResult, opts) };
      } catch (e) {
        if (!(e instanceof PageError)) throw e;
        return { scores: {}, failures: [{ category: "load", detail: e.message }] };
      }
    };
    let s;
    try {
      s = await sample();
    } catch (e) {
      apiErrors.push(`- ${path} (${strategy}): ${e.message}`);
      rows.push(`| ${path} | ${strategy} | ${Object.keys(TARGETS).map(() => "error").join(" | ")} | |`);
      continue;
    }
    // One sample can miss through noise on Google's side, so a page fails only when a second run
    // agrees. A second run the API fails leaves the first, failing, sample standing.
    if (s.failures.length) {
      try {
        s = await sample();
      } catch (e) {
        apiErrors.push(`- ${path} (${strategy}), second run: ${e.message}`);
      }
    }
    const { r, scores, failures } = s;
    if (r) {
      version = r.lighthouseResult.lighthouseVersion;
      origin ??= r.originLoadingExperience;
    }
    const cells = Object.keys(TARGETS).map((c) => (r ? pct(scores[c]) : "error"));
    rows.push(`| ${path} | ${strategy} | ${cells.join(" | ")} | ${r ? field(r.loadingExperience?.origin_fallback ? null : r.loadingExperience) : ""} |`);
    for (const f of failures) problems.push(`- ${path} (${strategy}) ${f.category}${f.audit ? ` \`${f.audit}\`` : ""}: ${f.detail}`);
    console.log(`${failures.length ? "✗" : "✓"} ${path} (${strategy}): ${r ? Object.entries(scores).map(([k, v]) => `${k} ${pct(v)}`).join(", ") : failures[0].detail}`);
  }
}

const report = [
  `PageSpeed Insights on ${base}, Lighthouse ${version || "unknown"}. Targets: performance ${pct(TARGETS.performance)} or more, every other category ${pct(1)}.`,
  "",
  `| Page | Form | ${Object.keys(TARGETS).join(" | ")} | Field data (this page) |`,
  `| --- | --- | ${Object.keys(TARGETS).map(() => "---").join(" | ")} | --- |`,
  ...rows,
  "",
  `Field data for the whole origin, mobile: ${field(origin)}.`,
  "",
  problems.length ? "## What failed\n\n" + problems.join("\n") : apiErrors.length ? "Every page the API returned met every target." : "Every page met every target.",
  "",
  ...(apiErrors.length ? ["## The API calls that failed", "", ...apiErrors, ""] : []),
].join("\n");
await writeFile("psi-report.md", report);
console.log(`\n${report}`);
if (apiErrors.length) console.error(`::error::the PageSpeed Insights API refused or failed ${apiErrors.length} call(s); check PSI_API_KEY and its quota`);
process.exit(problems.length ? 1 : apiErrors.length ? 3 : 0);
