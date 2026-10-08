// Holds built pages to WCAG 2.2 AAA: axe-core's rules through the AAA tags in both colour
// schemes, then the house checks in a11y-checks.mjs at 1280px, 2560px (the type must grow) and
// 320px (reflow, and the menu opened, closed and without script), and with the text-spacing
// override. docs/ACCESSIBILITY.md states the target and
// the regions held to AA. Usage: node scripts/a11y.mjs <dist> [path ...]. With no paths, a fixed
// set of representative pages is checked. Chrome is found at CHROME_PATH or /usr/bin/google-chrome.
import { createServer } from "node:http";
import { readFile, stat } from "node:fs/promises";
import { createRequire } from "node:module";
import { extname, join, normalize } from "node:path";
import puppeteer from "puppeteer-core";
import { HOUSE, TEXT_SPACING, house } from "./a11y-checks.mjs";

const require = createRequire(import.meta.url);
const AXE = require.resolve("axe-core/axe.min.js");
const TAGS = ["wcag2a", "wcag2aa", "wcag2aaa", "wcag21a", "wcag21aa", "wcag21aaa", "wcag22aa", "best-practice"];
const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css",
  ".js": "text/javascript",
  ".mjs": "text/javascript",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".woff2": "font/woff2",
  ".md": "text/markdown",
};
const DEFAULT = [
  "/",
  "/backlog/",
  "/browse/",
  "/agents/",
  "/publishers/",
  "/about/",
  "/terms/",
  "/accessibility/",
  "/privacy/",
  "/topics/roads/",
  "/d/qld-road-crash-locations/",
  "/d/qld-road-crash-locations/explore/",
  "/d/qld-road-crash-locations/in/gold-coast-city/",
  "/d/gnaf/",
  "/government/",
  "/c/qld-road-crashes/",
  "/qld/",
];
const WORK = { width: 1280, height: 900 };

const [dist, ...paths] = process.argv.slice(2);
if (!dist) {
  console.error("usage: node scripts/a11y.mjs <dist> [path ...]");
  process.exit(2);
}
const pages = paths.length ? paths : DEFAULT;

const server = createServer(async (req, res) => {
  let p = decodeURIComponent(new URL(req.url, "http://x").pathname);
  if (p.endsWith("/")) p += "index.html";
  const file = normalize(join(dist, p));
  try {
    if (!file.startsWith(normalize(dist))) throw new Error("outside");
    const s = await stat(file);
    if (s.isDirectory()) {
      res.writeHead(301, { location: p + "/" });
      return res.end();
    }
    res.writeHead(200, { "content-type": TYPES[extname(file)] || "application/octet-stream" });
    res.end(await readFile(file));
  } catch {
    res.writeHead(404);
    res.end("not found");
  }
});
await new Promise((r) => server.listen(0, "127.0.0.1", r));
const base = `http://127.0.0.1:${server.address().port}`;

const browser = await puppeteer.launch({
  executablePath: process.env.CHROME_PATH || "/usr/bin/google-chrome",
  args: ["--no-sandbox", "--disable-gpu"],
});
const axe = await readFile(AXE, "utf8");
const settle = (page) => page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
let failed = 0;
// Opens the menu from the keyboard, checks it, then closes it with Escape from its first link. Assumes the page
// is already at the narrow width.
async function menuRuns(page) {
  const sel = "header button[aria-controls][aria-expanded]";
  const toggle = await page.$$eval(sel, (bs) => bs.findIndex((b) => b.getBoundingClientRect().width > 0));
  if (toggle < 0) return [];
  const runs = [];
  await (await page.$$(sel))[toggle].focus();
  await page.keyboard.press("Enter");
  await settle(page);
  runs.push(await page.evaluate(house, HOUSE, "menu-open"));
  await page.evaluate((s) => {
    const b = [...document.querySelectorAll(s)].find((x) => x.getBoundingClientRect().width > 0);
    const first = document.getElementById(b.getAttribute("aria-controls"))?.querySelector("a[href], button");
    (first || b).focus();
  }, sel);
  await page.keyboard.press("Escape");
  await settle(page);
  runs.push(await page.evaluate(house, HOUSE, "menu-closed"));
  return runs;
}
async function noScriptRun(path) {
  const page = await browser.newPage();
  try {
    await page.setJavaScriptEnabled(false);
    await page.setViewport({ width: HOUSE.reflowWidth, height: 900 });
    await page.goto(base + path, { waitUntil: "load", timeout: 60000 });
    return await page.evaluate(house, HOUSE, "menu-nojs");
  } finally {
    await page.close();
  }
}
const report = (path, scheme, problems) => {
  if (!problems.length) {
    console.log(`✓ ${path} (${scheme})`);
    return;
  }
  failed++;
  console.log(`✗ ${path} (${scheme}): ${problems.length} problem(s)`);
  for (const p of problems) console.log(`  ${p}`);
};
try {
  for (const scheme of ["light", "dark"]) {
    for (const path of pages) {
      const page = await browser.newPage();
      await page.setViewport(WORK);
      await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: scheme }]);
      const resp = await page.goto(base + path, { waitUntil: "networkidle0", timeout: 60000 });
      if (!resp || resp.status() !== 200) {
        report(path, scheme, [`HTTP ${resp ? resp.status() : "none"}`]);
        await page.close();
        continue;
      }
      const problems = [];
      await page.evaluate(axe);
      const result = await page.evaluate(
        (tags) => window.axe.run(document, { runOnly: { type: "tag", values: tags } }),
        TAGS,
      );
      for (const v of result.violations) {
        problems.push(`axe ${v.id} [${v.impact}] ${v.help}`);
        for (const n of v.nodes.slice(0, 5)) {
          problems.push(`    ${n.target.join(" ")}`);
          problems.push(`      ${n.failureSummary.split("\n").join(" ").slice(0, 300)}`);
        }
        if (v.nodes.length > 5) problems.push(`    … and ${v.nodes.length - 5} more`);
      }
      // The house checks depend on layout and type, not colour, so one scheme is enough.
      if (scheme === "light") {
        const runs = [];
        runs.push(await page.evaluate(house, HOUSE, "all"));
        await page.setViewport({ width: HOUSE.wideWidth, height: 1440 });
        await settle(page);
        runs.push(await page.evaluate(house, HOUSE, "type"));
        await page.setViewport({ width: HOUSE.reflowWidth, height: 900 });
        await settle(page);
        runs.push(await page.evaluate(house, HOUSE, "reflow"));
        runs.push(...(await menuRuns(page)));
        await page.setViewport(WORK);
        await page.addStyleTag({ content: TEXT_SPACING });
        await settle(page);
        runs.push(await page.evaluate(house, HOUSE, "spacing"));
        runs.push(await noScriptRun(path));
        const byRule = new Map();
        for (const f of runs.flat()) {
          if (!byRule.has(f.rule)) byRule.set(f.rule, []);
          byRule.get(f.rule).push(f);
        }
        for (const [rule, fs] of byRule) {
          problems.push(`house ${rule}: ${fs.length} node(s)`);
          for (const f of fs.slice(0, 8)) problems.push(`    ${f.target}: ${f.detail}`);
          if (fs.length > 8) problems.push(`    … and ${fs.length - 8} more`);
        }
      }
      report(path, scheme, problems);
      await page.close();
    }
  }
} finally {
  await browser.close();
  server.close();
}
process.exit(failed ? 1 : 0);
