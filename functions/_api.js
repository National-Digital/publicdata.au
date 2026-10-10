import { SLUG, json } from './_lib.js';
import { LIMIT_DEFAULT, toCSV } from './_query.js';
import { AnswerError, WITHHELD, live, query } from './_answer.js';

const SITE = 'https://publicdata.au';
const API = `${SITE}/api/v1/datasets`;
// Answers are kept at the edge under their own URL with this marker added, so `publicdata purge`
// still clears a version's answers by its path, and none cached before one query path answered
// every version is served again. Raised when what a cached answer holds changes for the same URL.
const CACHE = '2';
const cacheKey = (url) =>
  new Request(`${url.origin}${url.pathname}${url.search ? url.search + '&' : '?'}_c=${CACHE}`);
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

// The query's own parameters, as the engines take them: format only shapes the reply.
const paramsOf = (url) =>
  url.search
    .slice(1)
    .split('&')
    .filter((p) => p && p.split('=')[0] !== 'format');

// Answers one query against a version: the one in the path, else the newest. Whichever engine
// answers, the reply has the same shape (functions/_answer.js). A dated answer never changes and
// is cached for good; the newest is cached for five minutes.
export async function answer(context, op) {
  const { request, params } = context;
  const url = new URL(request.url);
  const format = url.searchParams.get('format') || 'json';
  const key = cacheKey(url);
  const cache = caches.default;
  const hit = await cache.match(key);
  if (hit) {
    return hit;
  }
  if (!['json', 'ndjson', 'csv'].includes(format)) {
    return reply(400, { error: 'format is json, ndjson or csv' });
  }
  const slug = params.slug;
  const want = params.version;
  let a;
  try {
    a = await query(context, slug, want, op, paramsOf(url));
  } catch (e) {
    if (e instanceof AnswerError) {
      return reply(e.status, e.body);
    }
    throw e;
  }
  const rest = url.search;
  let next = null;
  if (a.more) {
    // The next page is pinned to this version's own path, so a release mid-way cannot shift it.
    const n = new URL(`${API}/${slug}/versions/${a.version}/${op}${rest}`);
    const offset = Number(url.searchParams.get('offset') || 0);
    const limit = Number(url.searchParams.get('limit') || LIMIT_DEFAULT);
    n.searchParams.set('offset', String(offset + limit));
    next = n.toString();
  }
  const cacheControl = want ? 'public, max-age=31536000, immutable' : 'public, max-age=300';
  const header = a.header || {};
  const manifest = `${SITE}/d/${slug}/v/${a.version}/manifest.json`;
  // CSV and NDJSON have nowhere to put the provenance header, so it travels in headers and the
  // version's manifest is linked; JSON carries it whole.
  const common = {
    ...POLICY,
    'access-control-allow-origin': '*',
    'cache-control': cacheControl,
    'x-publicdata-version': a.version,
    'x-publicdata-attribution': encodeURIComponent(a.attribution || header.attribution || ''),
    'x-publicdata-source-sha256': (header.source && header.source.sha256) || '',
    link: [
      `<${manifest}>; rel="describedby"`,
      ...(next ? [`<${next}>; rel="next"`] : []),
      TERMS,
    ].join(', '),
  };
  const rows = a.rows;
  let res;
  if (format === 'csv') {
    res = new Response(toCSV(a.cols, rows), {
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
        version_page: `${SITE}/d/${slug}/v/${a.version}/`,
        this_version: `${API}/${slug}/versions/${a.version}/${op}${rest}`,
        manifest,
        // The Parquet file or the period parts the answer was read from, when it came from them.
        ...(a.file ? { file: a.file } : {}),
        ...(a.parts ? { parts: a.parts } : {}),
        rows,
        next,
      }),
      { headers: { ...common, 'content-type': 'application/json; charset=utf-8' } },
    );
  }
  context.waitUntil(cache.put(key, res.clone()));
  return res;
}

// Every version of one dataset the query API answers, newest first, with the URLs to query each:
// each version in the dataset's versions.json, since the Parquet engine answers those D1 does not
// hold. A tombstoned version has no files left to answer from.
export async function versions(context) {
  const { request, env, params } = context;
  const slug = params.slug;
  if (!SLUG.test(slug)) {
    return reply(404, { error: 'No such dataset' });
  }
  if (!env.DB && !env.DIST) {
    return reply(503, { error: NOT_ENABLED });
  }
  const latest = await live(env);
  if (Object.keys(latest).length && !Object.hasOwn(latest, slug)) {
    return reply(410, { error: WITHHELD });
  }
  const key = cacheKey(new URL(request.url));
  const cache = caches.default;
  const hit = await cache.match(key);
  if (hit) {
    return hit;
  }
  let list = await published(env, slug);
  if (!list && env.DB) {
    try {
      list = await loadedVersions(env, slug);
    } catch (e) {
      if (!/no such table/i.test(String((e && e.message) || e))) {
        throw e;
      }
      if (!env.DIST) {
        return reply(503, { error: NOT_ENABLED });
      }
    }
  }
  if (!list || !list.length) {
    return reply(404, { error: 'No such dataset in the query API', files: `${SITE}/d/${slug}/` });
  }
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
  context.waitUntil(cache.put(key, res.clone()));
  return res;
}

async function loadedVersions(env, slug) {
  const r = await env.DB.prepare(
    'SELECT version, rows FROM _versions WHERE slug = ? ORDER BY version DESC',
  )
    .bind(slug)
    .all();
  return r.results || [];
}

// The versions of a dataset with a table the engines read, from the files the site was built
// with, or null when the deployment has no field list for it.
async function published(env, slug) {
  if (!env.ASSETS) {
    return null;
  }
  const at = (p) => env.ASSETS.fetch(new Request(`${SITE}/d/${slug}/${p}`));
  const [fields, vs] = await Promise.all([at('fields.json'), at('versions.json')]);
  if (!fields.ok || !vs.ok) {
    return null;
  }
  const body = await vs.json();
  return (body.versions || [])
    .filter((v) => !v.tombstone)
    .map((v) => ({ version: v.version, rows: v.rows }))
    .sort((a, b) => (a.version < b.version ? 1 : -1));
}
