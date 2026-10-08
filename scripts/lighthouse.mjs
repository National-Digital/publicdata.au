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
import { NOINDEX, PAGES, PREVIEW_SKIPS, TARGETS, assess, isPreview, medianRun } from "./lighthouse-checks.mjs";

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
if (preview) console.log(`${new URL(base).hostname} is a preview, served noindex, so ${PREVIEW_SKIPS.join(", ")} is not held here; production is.`);

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
  for (const [form, config] of Object.entries(FORMS)) {
    const runs = [];
    for (let i = 0; i < RUNS; i++) runs.push(await once(url, config));
    const lhr = medianRun(runs.map((r) => r.lhr));
    const html = runs.find((r) => r.lhr === lhr).html;
    summary.lighthouse = lhr.lighthouseVersion;
    const name = `${form}${path.replace(/[^a-z0-9]+/gi, "_")}`.replace(/_$/, "");
    await writeFile(join(out, `${name}.html`), html);
    const { scores, failures } = assess(lhr, { preview, noindex: NOINDEX.has(path) });
    const perfRuns = runs.map((r) => r.lhr.categories.performance?.score);
    summary.pages.push({ path, form, scores, performanceRuns: perfRuns, failures, report: `${name}.html` });
    const line = Object.entries(scores).map(([k, v]) => `${k} ${v === null ? "none" : Math.round(v * 100)}`).join(", ");
    if (failures.length) failed++;
    console.log(`${failures.length ? "✗" : "✓"} ${path} (${form}): ${line}; performance runs ${perfRuns.map((s) => Math.round(s * 100)).join("/")}`);
    for (const f of failures) console.log(`    ${f.category}${f.audit ? ` ${f.audit}` : ""}: ${f.detail}`);
  }
}
await writeFile(join(out, "summary.json"), JSON.stringify(summary, null, 2) + "\n");
process.exit(failed ? 1 : 0);
