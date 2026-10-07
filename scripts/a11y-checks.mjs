// The house accessibility checks, run inside a page. They cover what axe cannot: the size of the
// type, the length of a line, the size of a target, the name of a link, reflow and text spacing.
// A region marked data-conformance="aa" (dense data: tables, dataset listings, the explorer) is
// held to WCAG 2.2 AA for target size and type floor; everything else to AAA. The function must
// stay self-contained, because Puppeteer serialises it into the page.

export const HOUSE = {
  proseFloorRem: 0.875, // text a person reads, outside dense data regions
  anyFloorRem: 0.75, // every other visible text, and everything inside a dense data region
  rootAtWide: 19, // px at a 2560px viewport: the type must grow with the screen
  rootAtNarrow: 16,
  wideWidth: 2560,
  maxCharsPerLine: 80, // WCAG 1.4.8
  targetAAA: 44, // WCAG 2.5.5
  targetAA: 24, // WCAG 2.5.8, inside dense data regions
  reflowWidth: 320, // WCAG 1.4.10
  generic: [
    "here", "click here", "read more", "more", "learn more", "link", "details", "download", "open",
    "view", "all formats", "csv", "json", "parquet", "xlsx", "excel", "geojson", "sqlite", "duckdb",
    "arrow", "gpkg", "pmtiles", "ndjson", "csv.gz", "markdown",
  ],
};

// Runs in the page. `mode` picks the checks: "all" at the working width, "type" at the wide width,
// "reflow" at the narrow width, "spacing" after the text-spacing override has been injected.
export function house(cfg, mode) {
  const out = [];
  const add = (rule, el, detail) => out.push({ rule, target: label(el), detail });
  const label = (el) => {
    if (!el || !el.tagName) return String(el);
    const id = el.id ? "#" + el.id : "";
    const cls = el.classList && el.classList.length ? "." + [...el.classList].slice(0, 2).join(".") : "";
    const text = (el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 40);
    return `${el.tagName.toLowerCase()}${id}${cls}${text ? ` "${text}"` : ""}`;
  };
  const root = parseFloat(getComputedStyle(document.documentElement).fontSize);
  const visible = (el) => {
    if (!el || el.closest(".vh, [hidden], script, style, noscript, template")) return false;
    const s = getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const inAA = (el) => !!el.closest('[data-conformance="aa"]');
  const PROSE = "p, li, dd, dt, td, th, summary, label, figcaption, blockquote, h1, h2, h3, h4, h5, h6, a, button, input, select, textarea";
  const INTERACTIVE = 'a[href], button, input, select, textarea, summary, [role="button"], [role="tab"], [role="link"], [tabindex="0"]';

  if (mode === "all" || mode === "type") {
    const want = mode === "type" ? cfg.rootAtWide : cfg.rootAtNarrow;
    if (root < want) add("root-scale", document.documentElement, `root font-size ${root}px, expected at least ${want}px at ${innerWidth}px wide`);
    const seen = new Set();
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      if (!n.textContent.trim()) continue;
      const el = n.parentElement;
      if (!el || seen.has(el) || el.closest("svg") || !visible(el)) continue;
      seen.add(el);
      const rem = parseFloat(getComputedStyle(el).fontSize) / root;
      const floor = inAA(el) ? cfg.anyFloorRem : el.closest(PROSE) ? cfg.proseFloorRem : cfg.anyFloorRem;
      if (rem < floor - 0.001) add("type-floor", el, `${(rem * root).toFixed(1)}px is ${rem.toFixed(3)}rem, floor ${floor}rem`);
    }
    for (const el of document.querySelectorAll("p, li, dd, dt, blockquote")) {
      if (!visible(el) || inAA(el)) continue;
      if ([...el.children].some((c) => /^(block|flex|grid|table|list-item)/.test(getComputedStyle(c).display))) continue;
      const text = el.textContent.replace(/\s+/g, " ").trim();
      if (text.length < 120) continue;
      const range = document.createRange();
      range.selectNodeContents(el);
      const tops = new Set([...range.getClientRects()].filter((r) => r.width > 0).map((r) => Math.round(r.top / 4)));
      const lines = Math.max(1, tops.size);
      const cpl = Math.round(text.length / lines);
      if (cpl > cfg.maxCharsPerLine) add("measure", el, `${cpl} characters per line over ${lines} lines, maximum ${cfg.maxCharsPerLine}`);
    }
  }

  if (mode === "all") {
    const flows = /^(block|inline-block|list-item|table-cell)$/;
    for (const el of document.querySelectorAll(INTERACTIVE)) {
      if (!visible(el) || el.closest("svg")) continue;
      let box = el;
      if (el.matches('input[type="checkbox"], input[type="radio"]')) {
        box = el.closest("label") || (el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)) || el;
      }
      const s = getComputedStyle(el);
      // The inline exemption: a link in a sentence or block of text, reached through any inline
      // wrappers (b, em, span), whose container holds other text than the link's own.
      let wrapped = el;
      let container = el.parentElement;
      while (container && getComputedStyle(container).display === "inline") {
        wrapped = container;
        container = container.parentElement;
      }
      const inline = s.display === "inline" && container && flows.test(getComputedStyle(container).display) &&
        [...container.childNodes].some((c) => c !== wrapped && ((c.nodeType === 3 && c.textContent.trim()) || (c.nodeType === 1 && !c.matches(INTERACTIVE) && !c.querySelector(INTERACTIVE) && c.textContent.trim())));
      if (inline) continue;
      const rects = [...box.getClientRects()];
      const w = Math.max(...rects.map((r) => r.width), 0);
      const h = Math.max(...rects.map((r) => r.height), 0);
      const min = inAA(el) ? cfg.targetAA : cfg.targetAAA;
      if (w + 0.5 < min || h + 0.5 < min) add("target-size", el, `${Math.round(w)}x${Math.round(h)}px, minimum ${min}px${inAA(el) ? " (AA region)" : ""}`);
    }
    const generic = new Set(cfg.generic);
    for (const a of document.querySelectorAll("a[href]")) {
      if (!visible(a)) continue;
      let name = a.getAttribute("aria-label") || "";
      if (!name && a.getAttribute("aria-labelledby")) name = a.getAttribute("aria-labelledby").split(/\s+/).map((id) => (document.getElementById(id) || {}).textContent || "").join(" ");
      if (!name) name = a.textContent || [...a.querySelectorAll("img[alt], svg[aria-label]")].map((i) => i.getAttribute("alt") || i.getAttribute("aria-label")).join(" ") || a.title || "";
      name = name.replace(/\s+/g, " ").trim().toLowerCase();
      if (!name) add("link-purpose", a, "link has no accessible name");
      else if (generic.has(name)) add("link-purpose", a, `"${name}" does not say where the link goes`);
    }
    let focused = 0;
    for (const el of document.querySelectorAll(INTERACTIVE)) {
      if (focused >= 80) break;
      if (!visible(el) || el.closest("svg") || el.closest(".xhost")) continue;
      focused++;
      el.focus({ preventScroll: true });
      if (document.activeElement !== el) continue;
      const s = getComputedStyle(el);
      const ok = (s.outlineStyle !== "none" && parseFloat(s.outlineWidth) >= 2) || (s.boxShadow && s.boxShadow !== "none");
      if (!ok) add("focus-visible", el, `focused outline is ${s.outlineStyle} ${s.outlineWidth}`);
    }
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  }

  if (mode === "reflow") {
    const doc = document.documentElement;
    if (doc.scrollWidth > doc.clientWidth + 1) {
      const wide = [...document.body.querySelectorAll("*")].filter((el) => visible(el) && el.getBoundingClientRect().right > doc.clientWidth + 1 && getComputedStyle(el).position !== "fixed");
      add("reflow", wide[0] || doc, `page scrolls sideways at ${doc.clientWidth}px (${doc.scrollWidth}px wide); ${wide.length} elements overflow`);
    }
  }

  if (mode === "spacing") {
    const doc = document.documentElement;
    if (doc.scrollWidth > doc.clientWidth + 1) add("text-spacing", doc, "page scrolls sideways with the text-spacing override");
    for (const el of document.body.querySelectorAll("*")) {
      if (!visible(el) || inAA(el) || el.closest("svg, .xhost")) continue;
      const s = getComputedStyle(el);
      if (!/^(hidden|clip)$/.test(s.overflowX) && !/^(hidden|clip)$/.test(s.overflowY) && s.textOverflow !== "ellipsis") continue;
      if (s.webkitLineClamp && s.webkitLineClamp !== "none") continue;
      if (!el.textContent.trim()) continue;
      if (el.scrollWidth > el.clientWidth + 2 || el.scrollHeight > el.clientHeight + 2) add("text-spacing", el, `text is cut off with the text-spacing override (${el.scrollWidth}x${el.scrollHeight} in ${el.clientWidth}x${el.clientHeight})`);
    }
  }
  return out;
}

// WCAG 1.4.12's override, injected before the "spacing" pass.
export const TEXT_SPACING = "*{line-height:1.5!important;letter-spacing:.12em!important;word-spacing:.16em!important}p{margin-bottom:2em!important}";
