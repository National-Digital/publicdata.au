import { disposition } from '../_download.js';

// /d/* : serve the static file if Pages has it; redirect latest/ to the newest version;
// otherwise look in R2, which holds every dated version's files and anything over the Pages limit.
// A version's source.<ext> is the publisher's file, served from the raw store that keeps it.
const TYPES = {
  json: 'application/json; charset=utf-8', geojson: 'application/geo+json', ndjson: 'application/x-ndjson',
  csv: 'text/csv; charset=utf-8', parquet: 'application/vnd.apache.parquet', sqlite: 'application/vnd.sqlite3',
  zst: 'application/zstd', md: 'text/markdown; charset=utf-8', html: 'text/html; charset=utf-8', xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  arrow: 'application/vnd.apache.arrow.file', gpkg: 'application/geopackage+sqlite3', gz: 'application/gzip', sql: 'application/sql; charset=utf-8',
  pmtiles: 'application/vnd.pmtiles',
};

const DATED = /\/v\/\d{4}-\d{2}-\d{2}\//;
// The edge caches nothing larger than this, and until it has learnt that a file is too large it
// answers a byte range with the whole file, which hangs a range reader such as DuckDB.
const EDGE_MAX = 500 * 1024 * 1024;
const BYPASS = 'bypass';
// A version's page says whether it is the newest, so it changes and is cached briefly.
const PAGE = /\/v\/\d{4}-\d{2}-\d{2}\/(index\.(html|md))?$/;

// Pages applies _headers to its own files only, so a page read from R2 takes the site's
// security headers from the build, read once per isolate.
let pageHeaders;
async function headersFor(env) {
  if (pageHeaders) return pageHeaders;
  const r = await env.ASSETS.fetch(new Request('https://publicdata.au/static/page-headers.json'));
  if (r.ok) return (pageHeaders = await r.json());
  // Not remembered, so the next page tries again; the page still goes out with nosniff.
  console.error(`page-headers.json answered ${r.status}; a version page went out without the site's headers`);
  return { 'X-Content-Type-Options': 'nosniff' };
}

// The live datasets and their newest versions. An isolate serves one deployment, so it is read once.
let live;
async function liveOf(env, url) {
  if (live) return live;
  const r = await env.ASSETS.fetch(new URL('/latest.json', url));
  if (!r.ok) return {};
  return (live = await r.json());
}

// The publisher's files the register no longer republishes, which R2 still holds.
let held;
async function heldOf(env, url) {
  if (held) return held;
  const r = await env.ASSETS.fetch(new URL('/withheld.json', url));
  return (held = new Set(r.ok ? await r.json() : []));
}

// Whether R2 still holds versions of a withheld dataset, per slug. Its versions never change.
const archive = new Map();
async function archivedOf(env, slug) {
  if (!archive.has(slug)) archive.set(slug, (await env.DIST.list({ prefix: `d/${slug}/v/`, limit: 1 })).objects.length > 0);
  return archive.get(slug);
}

const SOURCE = /^d\/([a-z0-9-]+)\/v\/(\d{4}-\d{2}-\d{2})\/(source\.[a-z0-9]+)$/;

// The raw store also holds the bytes of a fetch that never became a version, so a file is served
// only beside a published version's manifest: in R2, or on Pages for a preview.
async function rawSource(env, url, key, read) {
  const m = key.match(SOURCE);
  if (!m || !env.RAW) return null;
  const man = `d/${m[1]}/v/${m[2]}/manifest.json`;
  let published = !!(await env.DIST.head(man));
  if (!published) {
    const r = await env.ASSETS.fetch(new Request(new URL('/' + man, url)));
    if (r.body) await r.body.cancel();
    published = r.ok;
  }
  // Pages binds R2 read-write only, so the archive is handed on with its two read methods alone.
  const raw = { get: (k, o) => env.RAW.get(k, o), head: (k) => env.RAW.head(k) };
  return published ? read(raw, `${m[1]}/${m[2]}/${m[3]}`) : null;
}

const WITHHELD = 'This file is withheld while its licence is reviewed. See https://publicdata.au/backlog/\n';

// Every published key is plain, so a path with escapes is sent to its plain form, where the
// withheld checks below read it as R2 and Pages would.
const PLAIN = /^[A-Za-z0-9._~/-]*$/;

export async function onRequestGet({ request, env }) {
  const url = new URL(request.url);
  if (url.pathname.includes('%')) {
    let plain = null;
    try { plain = decodeURIComponent(url.pathname); } catch { /* a malformed escape names nothing */ }
    if (!plain || !PLAIN.test(plain)) return new Response('Not found', { status: 404 });
    return new Response(null, { status: 308, headers: { location: plain + url.search, 'cache-control': 'public, max-age=86400' } });
  }
  const slug = (url.pathname.match(/^\/d\/([a-z0-9-]+)\//) || [])[1];
  const latest = slug ? await liveOf(env, url) : {};
  const r = await serve(request, env, url, latest);
  // R2 keeps every version of a dataset the register has since withheld, so a file it still holds
  // is refused here. An unread latest.json withholds nothing; a path with nothing behind it stays 404.
  const gone = slug && Object.keys(latest).length && !(slug in latest);
  // latest/ of a withheld dataset has no version to point at, so it is a 404 from serve; R2 still
  // holding versions says the dataset was withheld, not that it never existed.
  const archived = gone && r.status === 404 && /\/latest\//.test(url.pathname)
    && (await archivedOf(env, slug));
  if (slug && (archived || (r.status < 400 && (gone || (await heldOf(env, url)).has(url.pathname))))) {
    if (r.body) await r.body.cancel();
    return new Response(WITHHELD, { status: 410, headers: { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'public, max-age=300' } });
  }
  return r;
}

async function serve(request, env, url, latest) {
  const m = url.pathname.match(/^\/d\/([a-z0-9-]+)\/latest\/(.*)$/);
  if (m) {
    const v = latest[m[1]];
    if (!v) return new Response('No such dataset', { status: 404 });
    return new Response(null, {
      status: 302,
      headers: { location: `/d/${m[1]}/v/${v}/${m[2]}`, 'cache-control': 'public, max-age=300', 'access-control-allow-origin': '*' },
    });
  }
  // A dated version's page lives in R2. Pages can still answer its path with a copy from a
  // deployment that held it, so R2 is asked first.
  const page = PAGE.test(url.pathname);
  if (page) {
    const r = await fromR2(request, env, url);
    if (r) return r;
  }
  const asset = await env.ASSETS.fetch(request);
  if (asset.status !== 404) {
    // Pages _headers rules allow one splat, so the versioned tree gets its cache policy here.
    if (!DATED.test(url.pathname) || PAGE.test(url.pathname) || (asset.status !== 200 && asset.status !== 206)) return asset;
    const r = new Response(asset.body, asset);
    r.headers.set('cache-control', 'public, max-age=31536000, immutable, no-transform');
    const cd = disposition(decodeURIComponent(url.pathname.slice(1)));
    if (cd) r.headers.set('content-disposition', cd);
    return r;
  }
  return (!page && (await fromR2(request, env, url))) || asset;
}

async function fromR2(request, env, url) {
  const path = decodeURIComponent(url.pathname.replace(/^\//, ''));
  const key = path.endsWith('/') ? path + 'index.html' : path;
  const head = request.method === 'HEAD';
  const read = (bucket, k) => (head ? bucket.head(k) : bucket.get(k, { range: request.headers, onlyIf: request.headers }));
  const obj = (await read(env.DIST, key)) || (await rawSource(env, url, key, read));
  if (!obj) {
    // A version page asked for without its trailing slash, as Pages would redirect it.
    if (DATED.test(url.pathname + '/') && !/\.[a-z0-9]+$/i.test(key) && (await env.DIST.head(key + '/index.html'))) {
      return new Response(null, { status: 308, headers: { location: url.pathname + '/' + url.search } });
    }
    return null;
  }
  // A revalidation that will be answered 304 needs no bytes, so it is not sent on.
  const sending = head || ('body' in obj && obj.body);
  if (sending && obj.size > EDGE_MAX && DATED.test(key) && url.searchParams.get('edge') !== BYPASS) {
    // A file that large goes to its own URL marked edge=bypass, which a zone Cache Rule keeps
    // out of the cache, so every range reaches R2 and comes back as a 206.
    if (obj.body) await obj.body.cancel();
    const to = new URL(url);
    to.searchParams.set('edge', BYPASS);
    return new Response(null, {
      status: 302,
      headers: { location: to.href, 'cache-control': 'public, max-age=86400', 'access-control-allow-origin': '*' },
    });
  }
  const headers = new Headers();
  obj.writeHttpMetadata(headers);
  const ext = key.split('.').pop();
  if (TYPES[ext]) headers.set('content-type', TYPES[ext]);
  if (key.endsWith('.csv-metadata.json')) headers.set('content-type', 'application/csvm+json');
  headers.set('etag', obj.httpEtag);
  headers.set('accept-ranges', 'bytes');
  headers.set('access-control-allow-origin', '*');
  // no-transform keeps the edge from compressing the body, which would drop the byte range.
  const page = PAGE.test('/' + key);
  headers.set('cache-control', DATED.test(key) && !page ? 'public, max-age=31536000, immutable, no-transform' : 'public, max-age=300, no-transform');
  if (page) for (const [k, v] of Object.entries(await headersFor(env))) headers.set(k, v);
  const cd = disposition(key);
  if (cd) headers.set('content-disposition', cd);
  // Range readers such as DuckDB size the file from HEAD before asking for bytes.
  if (head) {
    headers.set('content-length', String(obj.size));
    return new Response(null, { status: 200, headers });
  }
  if ('body' in obj && obj.body) {
    // R2 reports a range for every read it was handed headers for, so only a Range request is partial.
    const partial = obj.range && request.headers.has('range');
    const status = partial ? 206 : 200;
    if (partial) {
      let start, end;
      if (obj.range.suffix !== undefined) { start = obj.size - obj.range.suffix; end = obj.size - 1; }
      else { start = obj.range.offset || 0; end = start + (obj.range.length || obj.size - start) - 1; }
      headers.set('content-range', `bytes ${start}-${end}/${obj.size}`);
    }
    return new Response(obj.body, { status, headers });
  }
  return new Response(null, { status: 304, headers });
}

export const onRequestHead = onRequestGet;
