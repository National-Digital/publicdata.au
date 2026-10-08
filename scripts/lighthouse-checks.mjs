// What a Lighthouse result must reach, shared by the pull request job (scripts/lighthouse.mjs) and
// the scheduled PageSpeed Insights check (scripts/psi.mjs). Pure functions of a Lighthouse result,
// so the Functions job can test them without Chrome or the lighthouse package.

// One page of each kind the site builds, each a slug the production build serves.
export const PAGES = [
  "/",
  "/backlog/",
  "/d/qld-road-crash-locations/",
  "/d/gnaf/",
  "/d/qld-road-crash-locations/explore/",
  "/d/qld-road-crash-locations/in/gold-coast-city/",
];

export const TARGETS = {
  performance: 0.9,
  accessibility: 1,
  "best-practices": 1,
  seo: 1,
  "agentic-browsing": 1,
};

// A Cloudflare Pages preview is served with x-robots-tag: noindex, so it can never pass this audit.
// Production is held to it, except on a page the site marks noindex on purpose. That page is
// named here, so an accidental noindex anywhere else still fails.
export const PREVIEW_SKIPS = ["is-crawlable"];
export const NOINDEX = new Set(["/d/qld-road-crash-locations/explore/"]);

export const isPreview = (url) => new URL(url).hostname.endsWith(".pages.dev");

const UNSCORED = new Set(["notApplicable", "manual", "informative", "error"]);

// Returns { scores, failures, skipped }. Performance is held to its category score. Every other
// category is held audit by audit, so a skipped audit cannot drag its category down and a category
// missing from the result fails rather than passing unseen.
export function assess(lhr, { preview = isPreview(lhr.finalDisplayedUrl || lhr.requestedUrl), noindex = NOINDEX.has(new URL(lhr.requestedUrl).pathname) } = {}) {
  const scores = {};
  const failures = [];
  const skipped = [];
  for (const [id, target] of Object.entries(TARGETS)) {
    const cat = lhr.categories?.[id];
    if (!cat) {
      failures.push({ category: id, detail: "category missing from the result" });
      continue;
    }
    scores[id] = cat.score;
    if (id === "performance") {
      if (cat.score === null || cat.score < target) {
        failures.push({ category: id, detail: `score ${fmt(cat.score)}, needs ${fmt(target)}` });
        for (const ref of cat.auditRefs) {
          const a = lhr.audits[ref.id];
          if (ref.weight > 0 && a && a.score !== null && a.score < 0.9) {
            failures.push({ category: id, audit: ref.id, detail: `${fmt(a.score)} ${a.displayValue || ""}`.trim() });
          }
        }
      }
      continue;
    }
    for (const ref of cat.auditRefs) {
      const a = lhr.audits[ref.id];
      if (!a || !(ref.weight > 0)) continue;
      if (a.scoreDisplayMode === "error") {
        failures.push({ category: id, audit: ref.id, detail: `audit errored: ${a.errorMessage || "no message"}` });
        continue;
      }
      if (UNSCORED.has(a.scoreDisplayMode) || a.score === null || a.score >= 1) continue;
      if ((preview || noindex) && PREVIEW_SKIPS.includes(ref.id)) {
        skipped.push(ref.id);
        continue;
      }
      failures.push({ category: id, audit: ref.id, detail: `${fmt(a.score)} ${a.title}${items(a)}` });
    }
  }
  return { scores, failures, skipped: [...new Set(skipped)] };
}

const fmt = (s) => (s === null || s === undefined ? "none" : String(Math.round(s * 100)));

function items(a) {
  const list = a.details?.items || [];
  if (!list.length) return "";
  const show = list.slice(0, 3).map((i) => String(i.node?.snippet || i.href || i.url || (i.element && i.issue ? `${i.element}: ${i.issue}` : i.element || i.issue) || i.text || JSON.stringify(i)).slice(0, 160));
  return `: ${show.join(" | ")}${list.length > 3 ? ` and ${list.length - 3} more` : ""}`;
}

// The run with the median performance score, as Lighthouse's own variability guidance advises.
export function medianRun(lhrs) {
  const sorted = [...lhrs].sort((a, b) => (a.categories.performance?.score ?? 0) - (b.categories.performance?.score ?? 0));
  return sorted[Math.floor((sorted.length - 1) / 2)];
}
