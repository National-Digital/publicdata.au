import { SLUG, json } from './_lib.js';
import { QueryError, toCSV } from './_query.js';

const SITE = 'https://publicdata.au';
const API = `${SITE}/api/v1/datasets`;
const VERSION = /^\d{4}-\d{2}-\d{2}$/;
// The fair-use limit the zone enforces on /api/v1/datasets/*, published on every answer.
const POLICY = {
  'ratelimit-policy': '"fair-use";q=60;w=10',
  'access-control-expose-headers':
    'Link, RateLimit-Policy, Retry-After, X-Publicdata-Version, X-Publicdata-Attribution, X-Publicdata-Source-Sha256',
};
// Every answer names the terms it is given under, so a program sees them without a page.
const TERMS = `<${SITE}/terms/>; rel="terms-of-service"`;
const reply = (status, body) => json(body, status, { ...POLICY, link: TERMS });
const NOT_ENABLED =
  'The query API is not enabled yet. Every dataset is available as files under /d/.';

async function newest(env, slug) {
  return env.DB.prepare('SELECT * FROM _versions WHERE slug = ? ORDER BY version DESC LIMIT 1')
    .bind(slug)
    .first();
}

async function loadedVersions(env, slug) {
  const r = await env.DB.prepare(
    'SELECT version, rows FROM _versions WHERE slug = ? ORDER BY version DESC',
  )
    .bind(slug)
    .all();
  return r.results || [];
}

// A dataset the register has withheld keeps its loaded tables until a load drops them, so the
// live list the site was built with decides. An unread list withholds nothing.
let live;
async function withheld(env, url, slug) {
  if (!env.ASSETS) return false;
  if (!live) {
    const r = await env.ASSETS.fetch(new URL('/latest.json', url));
    if (!r.ok) return false;
    live = await r.json();
  }
  return Object.keys(live).length > 0 && !(slug in live);
}
const WITHHELD =
  'This dataset is withheld while its licence is reviewed. See https://publicdata.au/backlog/';

function notLoaded(e) {
  // A database that has never been loaded has no registry yet.
  return /no such table/i.test(String((e && e.message) || e));
}

// Answers one query against a loaded version: the one in the path, else the newest. A dated
// answer never changes and is cached for good; the newest is cached for five minutes.
export async function answer(context, build, op) {
  const { request, env, params } = context;
  if (!env.DB) return reply(503, { error: NOT_ENABLED });
  const slug = params.slug;
  if (!SLUG.test(slug)) return reply(404, { error: 'No such dataset' });
  const url = new URL(request.url);
  if (await withheld(env, url, slug)) return reply(410, { error: WITHHELD });
  const qs = url.searchParams;
  const cache = caches.default;
  const hit = await cache.match(request);
  if (hit) return hit;
  const format = qs.get('format') || 'json';
  if (!['json', 'ndjson', 'csv'].includes(format))
    return reply(400, { error: 'format is json, ndjson or csv' });
  const want = params.version;
  if (want && !VERSION.test(want))
    return reply(404, {
      error: 'A version is a date, YYYY-MM-DD',
      versions: `${API}/${slug}/versions`,
    });
  let v;
  try {
    v = want
      ? await env.DB.prepare('SELECT * FROM _versions WHERE slug = ? AND version = ?')
          .bind(slug, want)
          .first()
      : await newest(env, slug);
  } catch (e) {
    if (notLoaded(e)) return reply(503, { error: NOT_ENABLED });
    throw e;
  }
  if (!v) {
    const list = (await loadedVersions(env, slug)).map((r) => r.version);
    return reply(404, {
      error: list.length
        ? `version ${want} is not loaded for queries; loaded versions are ${list.join(', ')}`
        : 'No such dataset in the query API',
      versions: `${API}/${slug}/versions`,
      files: `${SITE}/d/${slug}/`,
    });
  }
  let plan;
  try {
    plan = build(v.tbl, JSON.parse(v.fields), qs);
  } catch (e) {
    if (e instanceof QueryError) return reply(400, { error: e.message });
    throw e;
  }
  let rows;
  try {
    rows =
      (
        await env.DB.prepare(plan.sql)
          .bind(...plan.binds)
          .all()
      ).results || [];
  } catch (e) {
    return reply(500, {
      error: 'The query could not run',
      detail: String(e.message || e).slice(0, 200),
    });
  }
  const more = rows.length > plan.limit;
  if (more) rows = rows.slice(0, plan.limit);
  let next = null;
  if (more) {
    // The next page is pinned to this version's own path, so a release mid-way cannot shift it.
    const n = new URL(`${API}/${slug}/versions/${v.version}/${op}${url.search}`);
    n.searchParams.set('offset', String(plan.offset + plan.limit));
    next = n.toString();
  }
  const cacheControl = want ? 'public, max-age=31536000, immutable' : 'public, max-age=300';
  const header = JSON.parse(v.header || '{}');
  const manifest = `${SITE}/d/${slug}/v/${v.version}/manifest.json`;
  // CSV and NDJSON have nowhere to put the provenance header, so it travels in headers and the
  // version's manifest is linked; JSON carries it whole.
  const common = {
    ...POLICY,
    'access-control-allow-origin': '*',
    'cache-control': cacheControl,
    'x-publicdata-version': v.version,
    'x-publicdata-attribution': encodeURIComponent(v.attribution),
    'x-publicdata-source-sha256': (header.source && header.source.sha256) || '',
    link: [
      `<${manifest}>; rel="describedby"`,
      ...(next ? [`<${next}>; rel="next"`] : []),
      TERMS,
    ].join(', '),
  };
  let res;
  if (format === 'csv') {
    res = new Response(toCSV(plan.cols, rows), {
      headers: { ...common, 'content-type': 'text/csv; charset=utf-8' },
    });
  } else if (format === 'ndjson') {
    res = new Response(rows.map((r) => JSON.stringify(r)).join('\n') + (rows.length ? '\n' : ''), {
      headers: { ...common, 'content-type': 'application/x-ndjson' },
    });
  } else {
    res = new Response(
      JSON.stringify({
        publicdata: header,
        dataset_page: `${SITE}/d/${slug}/`,
        version_page: `${SITE}/d/${slug}/v/${v.version}/`,
        this_version: `${API}/${slug}/versions/${v.version}/${op}${url.search}`,
        manifest,
        rows,
        next,
      }),
      { headers: { ...common, 'content-type': 'application/json; charset=utf-8' } },
    );
  }
  context.waitUntil(cache.put(request, res.clone()));
  return res;
}

// The versions of one dataset loaded for queries, newest first, with the URLs to query each.
export async function versions(context) {
  const { request, env, params } = context;
  if (!env.DB) return reply(503, { error: NOT_ENABLED });
  const slug = params.slug;
  if (!SLUG.test(slug)) return reply(404, { error: 'No such dataset' });
  if (await withheld(env, new URL(request.url), slug)) return reply(410, { error: WITHHELD });
  const cache = caches.default;
  const hit = await cache.match(request);
  if (hit) return hit;
  let list;
  try {
    list = await loadedVersions(env, slug);
  } catch (e) {
    if (notLoaded(e)) return reply(503, { error: NOT_ENABLED });
    throw e;
  }
  if (!list.length)
    return reply(404, { error: 'No such dataset in the query API', files: `${SITE}/d/${slug}/` });
  const res = new Response(
    JSON.stringify({
      dataset: slug,
      dataset_page: `${SITE}/d/${slug}/`,
      versions: list.map((r) => ({
        version: r.version,
        rows: r.rows,
        rows_url: `${API}/${slug}/versions/${r.version}/rows`,
        aggregate_url: `${API}/${slug}/versions/${r.version}/aggregate`,
        files: `${SITE}/d/${slug}/v/${r.version}/`,
      })),
    }),
    {
      headers: {
        ...POLICY,
        link: TERMS,
        'access-control-allow-origin': '*',
        'cache-control': 'public, max-age=300',
        'content-type': 'application/json; charset=utf-8',
      },
    },
  );
  context.waitUntil(cache.put(request, res.clone()));
  return res;
}
