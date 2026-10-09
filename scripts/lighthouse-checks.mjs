// What a Lighthouse result must reach, shared by the pull request job (scripts/lighthouse.mjs) and
// the scheduled PageSpeed Insights check (scripts/psi.mjs). Pure functions of a Lighthouse result,
// so the Functions job can test them without Chrome or the lighthouse package.

// One page of each kind the site builds, each a slug the production build serves.
export const PAGES = [
  '/',
  '/backlog/',
  '/d/qld-road-crash-locations/',
  '/d/gnaf/',
  '/d/qld-road-crash-locations/explore/',
  '/d/qld-road-crash-locations/in/gold-coast-city/',
];

export const TARGETS = {
  performance: 0.9,
  accessibility: 1,
  'best-practices': 1,
  seo: 1,
  'agentic-browsing': 1,
};

// A Cloudflare Pages preview is served with x-robots-tag: noindex, so is-crawlable fails there on
// every page. Production is held to it, except on a page the site marks noindex on purpose with
// its own meta tag. Each is waived only when it is the audit's sole blocking directive, so a robots
// meta tag on any other page, or a robots.txt block, still fails.
export const PREVIEW_SKIPS = ['is-crawlable'];
export const NOINDEX = new Set(['/d/qld-road-crash-locations/explore/']);
export const PREVIEW_HEADER = 'x-robots-tag: noindex';
export const NOINDEX_META = /^<meta name="robots" content="noindex, follow"\s*\/?>$/;

// Whether every directive is-crawlable found is one this page is expected to carry.
function expectedNoindex(a, { preview, noindex }) {
  const found = a.details?.items || [];
  if (!found.length) {
    return false;
  }
  return found.every(
    (i) =>
      (preview && i.source === PREVIEW_HEADER) ||
      (noindex && NOINDEX_META.test(i.source?.snippet || '')),
  );
}

// The category's score with the waived audits counted as passed, as Lighthouse weighs them, so
// the noindex penalty is taken out exactly and every other audit still counts.
function held(lhr, cat, waived) {
  let sum = 0;
  let weight = 0;
  for (const ref of cat.auditRefs) {
    const a = lhr.audits[ref.id];
    if (!a || !(ref.weight > 0) || UNSCORED.has(a.scoreDisplayMode) || a.score === null) {
      continue;
    }
    sum += ref.weight * (waived.has(ref.id) ? 1 : a.score);
    weight += ref.weight;
  }
  return weight ? sum / weight : cat.score;
}

export const isPreview = (url) => new URL(url).hostname.endsWith('.pages.dev');

const UNSCORED = new Set(['notApplicable', 'manual', 'informative', 'error']);

// `skip` names further audits not to hold, such as one that read another host's file.
// Returns { scores, served, failures, skipped }. Performance is held to its category score. Every
// other category is held audit by audit and by its score with the waived audits taken out, which
// `scores` reports beside the score as served; a category missing from the result fails.
export function assess(
  lhr,
  {
    preview = isPreview(lhr.finalDisplayedUrl || lhr.requestedUrl),
    noindex = NOINDEX.has(new URL(lhr.requestedUrl).pathname),
    skip = [],
  } = {},
) {
  const scores = {};
  const served = {};
  const failures = [];
  const skipped = [];
  for (const [id, target] of Object.entries(TARGETS)) {
    const cat = lhr.categories?.[id];
    if (!cat) {
      failures.push({ category: id, detail: 'category missing from the result' });
      continue;
    }
    scores[id] = cat.score;
    served[id] = cat.score;
    if (id === 'performance') {
      if (cat.score === null || cat.score < target) {
        failures.push({ category: id, detail: `score ${fmt(cat.score)}, needs ${fmt(target)}` });
        for (const ref of cat.auditRefs) {
          const a = lhr.audits[ref.id];
          if (ref.weight > 0 && a && a.score !== null && a.score < 0.9) {
            failures.push({
              category: id,
              audit: ref.id,
              detail: `${fmt(a.score)} ${a.displayValue || ''}`.trim(),
            });
          }
        }
      }
      continue;
    }
    const waived = new Set();
    const before = failures.length;
    for (const ref of cat.auditRefs) {
      const a = lhr.audits[ref.id];
      if (!a || !(ref.weight > 0)) {
        continue;
      }
      if (a.scoreDisplayMode === 'error') {
        failures.push({
          category: id,
          audit: ref.id,
          detail: `audit errored: ${a.errorMessage || 'no message'}`,
        });
        continue;
      }
      if (UNSCORED.has(a.scoreDisplayMode) || a.score === null || a.score >= 1) {
        continue;
      }
      if (
        (PREVIEW_SKIPS.includes(ref.id) && expectedNoindex(a, { preview, noindex })) ||
        skip.includes(ref.id)
      ) {
        skipped.push(ref.id);
        waived.add(ref.id);
        continue;
      }
      failures.push({
        category: id,
        audit: ref.id,
        detail: `${fmt(a.score)} ${a.title}${items(a)}`,
      });
    }
    if (waived.size) {
      scores[id] = held(lhr, cat, waived);
    }
    if (failures.length === before && scores[id] !== null && scores[id] < target - 1e-9) {
      failures.push({
        category: id,
        detail: `score ${fmt(scores[id])} with ${[...waived].join(', ')} taken out, needs ${fmt(target)}`,
      });
    }
  }
  return { scores, served, failures, skipped: [...new Set(skipped)] };
}

const fmt = (s) => (s === null || s === undefined ? 'none' : String(Math.round(s * 100)));

function items(a) {
  const list = a.details?.items || [];
  if (!list.length) {
    return '';
  }
  const show = list
    .slice(0, 3)
    .map((i) =>
      String(
        i.node?.snippet ||
          i.href ||
          i.url ||
          (i.element && i.issue ? `${i.element}: ${i.issue}` : i.element || i.issue) ||
          i.text ||
          JSON.stringify(i),
      ).slice(0, 160),
    );
  return `: ${show.join(' | ')}${list.length > 3 ? ` and ${list.length - 3} more` : ''}`;
}

// The ARD manifest Lighthouse 13.5's gatherer reads for a page, found in its order: the robots.txt
// Agentmap line, then <link rel="ai-catalog">, then a Link header, then the well-known path. Each is
// resolved against the page, so an absolute URL in any of them can point a preview at production.
export function ardManifest(pageUrl, { robotsTxt = '', html = '', linkHeader = '' } = {}) {
  const agentmap = robotsTxt.match(/^\s*Agentmap:\s*(\S+)/im)?.[1];
  const link = html
    .match(/<link\b[^>]*\brel=["']?[^"'>]*\bai-catalog\b[^>]*>/i)?.[0]
    ?.match(/\bhref=["']?([^"'\s>]+)/i)?.[1];
  const header = linkHeader.match(/<([^>]+)>\s*;[^,]*\brel="?[^",]*\bai-catalog\b/i)?.[1];
  return new URL(agentmap || link || header || '/.well-known/ai-catalog.json', pageUrl).href;
}

// The run with the median performance score, as Lighthouse's own variability guidance advises.
export function medianRun(lhrs) {
  const sorted = [...lhrs].sort(
    (a, b) => (a.categories.performance?.score ?? 0) - (b.categories.performance?.score ?? 0),
  );
  return sorted[Math.floor((sorted.length - 1) / 2)];
}
