// Runs Lighthouse over a deployed site, mobile and desktop, three times a page, and holds the run
// with the median performance score to the targets in lighthouse-checks.mjs. A pull request runs it
// against its own preview deploy, which serves the real headers, CSP and compression; a local
// server would not. Usage: node scripts/lighthouse.mjs <base-url> [path ...]. Writes summary.json
// and one HTML report per page and form factor to LIGHTHOUSE_OUT (default lighthouse-reports/).
// Chrome is found at CHROME_PATH or /usr/bin/google-chrome.
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";
import lighthouse from "lighthouse";
import desktopConfig from "lighthouse/core/config/desktop-config.js";
import puppeteer from "puppeteer-core";
import { NOINDEX, PAGES, PREVIEW_HEADER, PREVIEW_SKIPS, TARGETS, ardManifest, assess, isPreview, medianRun } from "./lighthouse-checks.mjs";

const RUNS = Number(process.env.LIGHTHOUSE_RUNS || 3);
const FORMS = { mobile: undefined, desktop: desktopConfig };

const [base, ...paths] = process.argv.slice(2);
if (!base) {
  console.error("usage: node scripts/lighthouse.mjs <base-url> [path ...]");
  process.exit(2);
}
const out = process.env.LIGHTHOUSE_OUT || "lighthouse-reports";
await mkdir(out, { recursive: true });
const preview = isPreview(base);
if (preview) console.log(`${new URL(base).hostname} is a preview, served ${PREVIEW_HEADER}, so ${PREVIEW_SKIPS.join(", ")} is waived for that header alone and the category scored without it; production holds it.`);

// ard-schema audits the manifest the page points to. When that is on another host, as when the
// build names production in absolute URLs, the audit is not about this deploy, so it is left to the
// production run (psi.yml) and the output says so. llms-txt always reads /llms.txt on the page's
// own host, so it is held here.
const robotsTxt = await fetch(new URL("/robots.txt", base)).then((r) => (r.ok ? r.text() : ""), () => "");
async function offHostSkips(url) {
  const res = await fetch(url);
  const manifest = ardManifest(url, { robotsTxt, html: await res.text(), linkHeader: res.headers.get("link") || "" });
  return new URL(manifest).host === new URL(url).host ? { skip: [], manifest } : { skip: ["ard-schema"], manifest };
}

// A fresh browser per run, so no run inherits another's cache.
async function once(url, config) {
  const browser = await puppeteer.launch({
    executablePath: process.env.CHROME_PATH || "/usr/bin/google-chrome",
    args: ["--no-sandbox", "--disable-gpu"],
  });
  try {
    const page = await browser.newPage();
    const result = await lighthouse(url, { output: "html", logLevel: "error" }, config, page);
    return { lhr: result.lhr, html: result.report };
  } finally {
    await browser.close();
  }
}

const summary = { base, preview, skipped: preview ? PREVIEW_SKIPS : [], targets: TARGETS, lighthouse: null, pages: [] };
let failed = 0;
for (const path of paths.length ? paths : PAGES) {
  const url = new URL(path, base).href;
  const { skip, manifest } = await offHostSkips(url);
  if (skip.length) console.log(`${path}: ard-schema reads ${manifest}, another host's manifest, so it is left to the production run.`);
  for (const [form, config] of Object.entries(FORMS)) {
    const runs = [];
    for (let i = 0; i < RUNS; i++) runs.push(await once(url, config));
    const lhr = medianRun(runs.map((r) => r.lhr));
    const html = runs.find((r) => r.lhr === lhr).html;
    summary.lighthouse = lhr.lighthouseVersion;
    const name = `${form}${path.replace(/[^a-z0-9]+/gi, "_")}`.replace(/_$/, "");
    await writeFile(join(out, `${name}.html`), html);
    const { scores, served, failures } = assess(lhr, { preview, noindex: NOINDEX.has(path), skip });
    const perfRuns = runs.map((r) => r.lhr.categories.performance?.score);
    summary.pages.push({ path, form, scores, served, performanceRuns: perfRuns, failures, ardManifest: manifest, notHeld: skip, report: `${name}.html` });
    const pct = (v) => (v === null ? "none" : Math.round(v * 100));
    const line = Object.entries(scores).map(([k, v]) => `${k} ${pct(v)}${v === served[k] ? "" : ` (${pct(served[k])} as served)`}`).join(", ");
    if (failures.length) failed++;
    console.log(`${failures.length ? "✗" : "✓"} ${path} (${form}): ${line}; performance runs ${perfRuns.map((s) => Math.round(s * 100)).join("/")}`);
    for (const f of failures) console.log(`    ${f.category}${f.audit ? ` ${f.audit}` : ""}: ${f.detail}`);
  }
}
await writeFile(join(out, "summary.json"), JSON.stringify(summary, null, 2) + "\n");
process.exit(failed ? 1 : 0);
