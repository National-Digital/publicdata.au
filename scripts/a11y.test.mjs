import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { test } from "node:test";
import { HOUSE, TEXT_SPACING, exceptionsFor, house, paintedContrast } from "./a11y-checks.mjs";

// Each house rule is proven to fire on the defect it guards against, and to stay quiet on a page
// without it. The browser is the same one the gate uses; a job without Chrome or without the
// npm packages (the Functions job runs every script test with neither) skips rather than fails.
const chrome = process.env.CHROME_PATH || "/usr/bin/google-chrome";
const puppeteer = await import("puppeteer-core").then((m) => m.default).catch(() => null);
const skip = !puppeteer ? "puppeteer-core is not installed" : existsSync(chrome) ? false : `no Chrome at ${chrome}`;

const BASE = `<!doctype html><meta charset="utf-8"><style>
html{font-size:clamp(100%,.75rem + .35vw,125%)}body{margin:0;font:1rem/1.55 Arial,sans-serif}button{font:inherit}
.site{max-width:70rem;padding:1rem}p{max-width:65ch}:focus-visible{outline:2px solid #00f;outline-offset:2px}
a.big,button{display:inline-flex;min-height:2.75rem;min-width:2.75rem;align-items:center;padding:0 1rem}
</style><body><div class="site"><main>`;
const END = `</main></div>`;
const LONG = "Words ".repeat(60).trim();

async function run(html, mode, width = 1280) {
  const browser = await puppeteer.launch({ executablePath: chrome, args: ["--no-sandbox", "--disable-gpu"] });
  try {
    const page = await browser.newPage();
    if (mode === "menu-nojs") await page.setJavaScriptEnabled(false);
    await page.setViewport({ width, height: 900 });
    await page.setContent(html, { waitUntil: "load" });
    if (mode === "spacing") await page.addStyleTag({ content: TEXT_SPACING });
    if (mode === "menu-open" || mode === "menu-closed") {
      await page.focus("header button");
      await page.keyboard.press("Enter");
    }
    if (mode === "menu-closed") {
      await page.focus("nav a");
      await page.keyboard.press("Escape");
    }
    return await page.evaluate(house, HOUSE, mode);
  } finally {
    await browser.close();
  }
}
const rules = (found) => [...new Set(found.map((f) => f.rule))].sort();

test("a clean page raises nothing at any width", { skip }, async () => {
  const html = `${BASE}<h1>Title</h1><p>${LONG}</p><p><a href="/x">Where this goes</a> in a sentence.</p><a class="big" href="/y">A big enough link</a><button type="button">Act</button>${END}`;
  assert.deepEqual(await run(html, "all"), []);
  assert.deepEqual(await run(html, "type", HOUSE.wideWidth), []);
  assert.deepEqual(await run(html, "reflow", HOUSE.reflowWidth), []);
  assert.deepEqual(await run(html, "spacing"), []);
});

test("small type fails outside a data region and passes inside one at the AA floor", { skip }, async () => {
  const bad = await run(`${BASE}<p style="font-size:.8rem">${LONG}</p><span style="font-size:.7rem">tiny label</span>${END}`, "all");
  assert.deepEqual(rules(bad), ["type-floor"]);
  assert.equal(bad.length, 2);
  const aa = await run(`${BASE}<div data-conformance="aa"><table><tr><td style="font-size:.8rem">cell</td></tr></table></div>${END}`, "all");
  assert.deepEqual(aa, []);
});

test("type that does not grow with the screen fails the wide pass", { skip }, async () => {
  const fixed = `${BASE.replace("html{font-size:clamp(100%,.75rem + .35vw,125%)}", "html{font-size:16px}")}<p>${LONG}</p>${END}`;
  assert.deepEqual(rules(await run(fixed, "type", HOUSE.wideWidth)), ["root-scale"]);
});

test("a line over 80 characters fails the measure", { skip }, async () => {
  const found = await run(`${BASE}<p style="max-width:none">${LONG}</p>${END}`, "all");
  assert.deepEqual(rules(found), ["measure"]);
});

test("targets under 44px fail, under 24px fail inside a data region, inline links in text are exempt", { skip }, async () => {
  const found = await run(
    `${BASE}<div><a href="/a" style="display:inline-block;padding:2px">pill</a><a href="/b" style="display:inline-block;padding:2px">pill</a></div>
     <div data-conformance="aa"><div><a href="/c" style="display:inline-block;padding:0;line-height:1">tiny</a><a href="/d" style="display:inline-block;padding:4px 8px;line-height:1.2">fine</a></div></div>
     <p>A sentence with <a href="/e">an inline link</a> in it.</p>${END}`,
    "all",
  );
  const targets = found.filter((f) => f.rule === "target-size");
  assert.equal(targets.length, 3, JSON.stringify(found));
  assert.ok(targets.some((f) => f.detail.includes("(AA region)")));
});

test("a checkbox is measured by its label", { skip }, async () => {
  const ok = await run(`${BASE}<label style="display:inline-flex;min-height:2.75rem;min-width:8rem;align-items:center"><input type="checkbox"> Only open ones</label>${END}`, "all");
  assert.deepEqual(ok, []);
  const bad = await run(`${BASE}<input type="checkbox" id="c"><label for="c" style="display:inline-block;line-height:1">Open</label>${END}`, "all");
  assert.deepEqual(rules(bad), ["target-size"]);
});

test("a link whose name says nothing fails, an aria-label that names the dataset passes", { skip }, async () => {
  const bad = await run(`${BASE}<a class="big" href="/x.csv">csv</a><a class="big" href="/y">Download</a><a class="big" href="/z"><img src="" alt=""></a>${END}`, "all");
  assert.deepEqual(rules(bad), ["link-purpose"]);
  assert.equal(bad.length, 3);
  const ok = await run(`${BASE}<a class="big" href="/x.csv" aria-label="CSV, Road crashes">csv</a>${END}`, "all");
  assert.deepEqual(ok, []);
});

test("a focus style that is removed fails", { skip }, async () => {
  const found = await run(`${BASE}<button type="button" style="outline:none">Act</button>${END}`, "all");
  assert.deepEqual(rules(found), ["focus-visible"]);
});

test("content wider than 320px fails reflow", { skip }, async () => {
  const found = await run(`${BASE}<div style="width:600px;height:10px;background:#ccc"></div>${END}`, "reflow", HOUSE.reflowWidth);
  assert.deepEqual(rules(found), ["reflow"]);
});

test("text cut off by the spacing override fails, a line clamp or a data region does not", { skip }, async () => {
  const bad = await run(`${BASE}<div style="width:8rem;height:1.2rem;overflow:hidden;white-space:nowrap">${LONG}</div>${END}`, "spacing");
  assert.deepEqual(rules(bad), ["text-spacing"]);
  const ok = await run(`${BASE}<div data-conformance="aa"><div style="width:8rem;height:1.2rem;overflow:hidden;white-space:nowrap">${LONG}</div></div>${END}`, "spacing");
  assert.deepEqual(ok, []);
});

const menuPage = ({ escape, links, nojs }) => `${BASE.replace("</style>", `nav{display:none}header.open nav{display:flex;flex-direction:column}nav a{${links}}${nojs ? "@media (scripting:none){nav{display:flex}}" : ""}</style>`)}
<header><button type="button" aria-expanded="false" aria-controls="n">Menu</button><nav id="n" aria-label="Site"><a href="/a">Datasets</a><a href="/b">About</a></nav></header>
<script>var b=document.querySelector("header button"),h=b.parentElement;b.onclick=function(){var on=b.getAttribute("aria-expanded")!=="true";b.setAttribute("aria-expanded",on);h.classList.toggle("open",on)};
${escape ? 'h.onkeydown=function(e){if(e.key==="Escape"){b.setAttribute("aria-expanded","false");h.classList.remove("open");b.focus()}};' : ""}</script>${END}`;

test("a menu that opens to full-size links, closes on Escape and shows its links without script passes", { skip }, async () => {
  const html = menuPage({ escape: true, links: "min-height:2.75rem;display:flex;align-items:center", nojs: true });
  assert.deepEqual(await run(html, "menu-open", HOUSE.reflowWidth), []);
  assert.deepEqual(await run(html, "menu-closed", HOUSE.reflowWidth), []);
  assert.deepEqual(await run(html, "menu-nojs", HOUSE.reflowWidth), []);
});

test("a menu with small links, no Escape and nothing without script fails each pass", { skip }, async () => {
  const html = menuPage({ escape: false, links: "display:block;line-height:1", nojs: false });
  assert.deepEqual(rules(await run(html, "menu-open", HOUSE.reflowWidth)), ["target-size"]);
  const closed = await run(html, "menu-closed", HOUSE.reflowWidth);
  assert.deepEqual(rules(closed), ["menu"]);
  assert.ok(closed.some((f) => f.detail.includes("Escape does not close")));
  assert.deepEqual(rules(await run(html, "menu-nojs", HOUSE.reflowWidth)), ["menu"]);
});

async function painted(html, targets) {
  const browser = await puppeteer.launch({ executablePath: chrome, args: ["--no-sandbox", "--disable-gpu"] });
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1280, height: 900 });
    await page.setContent(html, { waitUntil: "load" });
    return await paintedContrast(page, targets);
  } finally {
    await browser.close();
  }
}

test("text on a gradient fails where the gradient's worst stop is too light, and passes where every stop clears 7:1", { skip }, async () => {
  const html = `${BASE}<div style="padding:1rem;background:linear-gradient(90deg,#000,#000 50%,#888)"><p id="bad" style="color:#fff;max-width:none">${LONG}</p></div>
    <div style="padding:1rem;background:linear-gradient(90deg,#000,#1a1a1a)"><p id="good" style="color:#fff;max-width:none">${LONG}</p></div>${END}`;
  const found = await painted(html, ["#bad", "#good"]);
  assert.deepEqual(found.map((f) => f.target), ["#bad"]);
  assert.ok(found[0].ratio < 7);
});

test("a halo drawn as the text's own stroke counts as its background", { skip }, async () => {
  const svg = (halo) => `<svg width="300" height="60"><rect width="300" height="60" fill="#777"/><text id="t" x="10" y="40" font-size="20" fill="#fff" ${halo ? 'stroke="#000" stroke-width="6" paint-order="stroke"' : ""}>Cairns harbour</text></svg>`;
  assert.deepEqual(await painted(`${BASE}${svg(true)}${END}`, ["#t"]), []);
  assert.equal((await painted(`${BASE}${svg(false)}${END}`, ["#t"])).length, 1);
});

test("an exception covers its rule on the pages its pattern names and nowhere else", () => {
  const ex = [{ pages: "/d/*/explore/", rule: "painted-contrast", within: "#x-ghost" }];
  assert.equal(exceptionsFor(ex, "/d/qld-road-crash-locations/explore/", "painted-contrast").length, 1);
  assert.equal(exceptionsFor(ex, "/d/qld-road-crash-locations/", "painted-contrast").length, 0);
  assert.equal(exceptionsFor(ex, "/d/a/b/explore/", "painted-contrast").length, 0);
  assert.equal(exceptionsFor(ex, "/d/qld-road-crash-locations/explore/", "color-contrast").length, 0);
});
