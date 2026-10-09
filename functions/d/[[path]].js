import { disposition } from '../_download.js';
import { gunzip, gzipped } from '../_lib.js';

// /d/* : serve the static file if Pages has it; redirect latest/ to the newest version, or for a
// rolling source or a feed, serve its newest fetch in place from latest/;
// otherwise look in R2, which holds every dated version's files and anything over the Pages limit.
// A version's source.<ext> is the publisher's file, served from the raw store that keeps it.
const TYPES = {
  json: 'application/json; charset=utf-8',
  geojson: 'application/geo+json',
  ndjson: 'application/x-ndjson',
  csv: 'text/csv; charset=utf-8',
  parquet: 'application/vnd.apache.parquet',
  sqlite: 'application/vnd.sqlite3',
  zst: 'application/zstd',
  md: 'text/markdown; charset=utf-8',
  html: 'text/html; charset=utf-8',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  arrow: 'application/vnd.apache.arrow.file',
  gpkg: 'application/geopackage+sqlite3',
  gz: 'application/gzip',
  sql: 'application/sql; charset=utf-8',
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
  if (pageHeaders) {
    return pageHeaders;
  }
  const r = await env.ASSETS.fetch(new Request('https://publicdata.au/static/page-headers.json'));
  if (r.ok) {
    pageHeaders = await r.json();
    return pageHeaders;
  }
  // Not remembered, so the next page tries again; the page still goes out with nosniff.
  // eslint-disable-next-line no-console -- the Workers log is where an operator sees this.
  console.error(
    `page-headers.json answered ${r.status}; a version page went out without the site's headers`,
  );
  return { 'X-Content-Type-Options': 'nosniff' };
}

// The live datasets and their newest versions. An isolate serves one deployment, so it is read once.
let live;
async function liveOf(env, url) {
  if (live) {
    return live;
  }
  const r = await env.ASSETS.fetch(new URL('/latest.json', url));
  if (!r.ok) {
    return {};
  }
  live = await r.json();
  return live;
}

// The rolling sources and feeds, whose latest/ is built in place from their newest fetch.
let current;
async function currentOf(env, url) {
  if (current) {
    return current;
  }
  const r = await env.ASSETS.fetch(new URL('/current.json', url));
  current = r.ok ? await r.json() : {};
  return current;
}

// The publisher's files the register no longer republishes, which R2 still holds.
let held;
async function heldOf(env, url) {
  if (held) {
    return held;
  }
  const r = await env.ASSETS.fetch(new URL('/withheld.json', url));
  held = new Set(r.ok ? await r.json() : []);
  return held;
}

// Whether R2 still holds versions of a withheld dataset, per slug. Its versions never change.
const archive = new Map();
async function archivedOf(env, slug) {
  if (!archive.has(slug)) {
    archive.set(
      slug,
      (await env.DIST.list({ prefix: `d/${slug}/v/`, limit: 1 })).objects.length > 0,
    );
  }
  return archive.get(slug);
}

const SOURCE = /^d\/([a-z0-9-]+)\/v\/(\d{4}-\d{2}-\d{2})\/(source\.[a-z0-9]+)$/;

// The raw store also holds the bytes of a fetch that never became a version, so a file is served
// only beside a published version's manifest: in R2, or on Pages for a preview.
async function rawSource(env, url, key, read) {
  const m = key.match(SOURCE);
  if (!m || !env.RAW) {
    return null;
  }
  const man = `d/${m[1]}/v/${m[2]}/manifest.json`;
  let published = !!(await env.DIST.head(man));
  if (!published) {
    const r = await env.ASSETS.fetch(new Request(new URL('/' + man, url)));
    if (r.body) {
      await r.body.cancel();
    }
    published = r.ok;
  }
  // Pages binds R2 read-write only, so the archive is handed on with its two read methods alone.
  const raw = { get: (k, o) => env.RAW.get(k, o), head: (k) => env.RAW.head(k) };
  return published ? read(raw, `${m[1]}/${m[2]}/${m[3]}`) : null;
}

const WITHHELD =
  'This file is withheld while its licence is reviewed. See https://publicdata.au/backlog/\n';

// Every published key is plain, so a path with escapes is sent to its plain form, where the
// withheld checks below read it as R2 and Pages would.
const PLAIN = /^[A-Za-z0-9._~/-]*$/;

export async function onRequestGet({ request, env }) {
  const url = new URL(request.url);
  if (url.pathname.includes('%')) {
    let plain = null;
    try {
      plain = decodeURIComponent(url.pathname);
    } catch {
      /* a malformed escape names nothing */
    }
    if (!plain || !PLAIN.test(plain)) {
      return new Response('Not found', { status: 404 });
    }
    return new Response(null, {
      status: 308,
      headers: { location: plain + url.search, 'cache-control': 'public, max-age=86400' },
    });
  }
  const slug = (url.pathname.match(/^\/d\/([a-z0-9-]+)\//) || [])[1];
  const latest = slug ? await liveOf(env, url) : {};
  const r = await serve(request, env, url, latest, slug ? await currentOf(env, url) : {});
  // R2 keeps every version of a dataset the register has since withheld, so a file it still holds
  // is refused here. An unread latest.json withholds nothing; a path with nothing behind it stays 404.
  const gone = slug && Object.keys(latest).length && !(slug in latest);
  // latest/ of a withheld dataset has no version to point at, so it is a 404 from serve; R2 still
  // holding versions says the dataset was withheld, not that it never existed.
  const archived =
    gone && r.status === 404 && /\/latest\//.test(url.pathname) && (await archivedOf(env, slug));
  if (
    slug &&
    (archived || (r.status < 400 && (gone || (await heldOf(env, url)).has(url.pathname))))
  ) {
    if (r.body) {
      await r.body.cancel();
    }
    return new Response(WITHHELD, {
      status: 410,
      headers: {
        'content-type': 'text/plain; charset=utf-8',
        'cache-control': 'public, max-age=300',
      },
    });
  }
  return r;
}

async function serve(request, env, url, latest, inPlace) {
  const m = url.pathname.match(/^\/d\/([a-z0-9-]+)\/latest\/(.*)$/);
  // latest/ itself has no page of its own, so it still goes to the newest snapshot's page.
  if (m && m[2] && m[1] in inPlace && m[1] in latest) {
    // The newest fetch is built under a folder of its own date, which current.json names, so a
    // file left from an older fetch is never served, and a deploy half out never mixes two.
    const cur = inPlace[m[1]];
    const source = cur.source && m[2] === cur.source;
    const key = `d/${m[1]}/fetch/${cur.fetch}/${m[2]}`;
    let r;
    if (source) {
      const head = request.method === 'HEAD';
      const raw = `${m[1]}/${cur.fetch}/${m[2]}`;
      const obj =
        env.RAW &&
        (await (head
          ? env.RAW.head(raw)
          : env.RAW.get(raw, { range: request.headers, onlyIf: request.headers })));
      r = obj ? await answer(request, url, key, obj, head, env, latest) : null;
    } else {
      const asset = await env.ASSETS.fetch(new Request(new URL('/' + key, url), request));
      r =
        asset.status !== 404 ? asset : await fromR2(request, env, new URL('/' + key, url), latest);
    }
    if (!r || r.status >= 400) {
      return r && r.status !== 404 ? r : new Response('Not found', { status: 404 });
    }
    const out = new Response(r.body, r);
    out.headers.set('cache-control', 'public, max-age=300, no-transform');
    out.headers.set('access-control-allow-origin', '*');
    const cd = disposition(key, latest);
    if (cd) {
      out.headers.set('content-disposition', cd);
    }
    return out;
  }
  if (m) {
    const v = latest[m[1]];
    if (!v) {
      return new Response('No such dataset', { status: 404 });
    }
    return new Response(null, {
      status: 302,
      headers: {
        location: `/d/${m[1]}/v/${v}/${m[2]}`,
        'cache-control': 'public, max-age=300',
        'access-control-allow-origin': '*',
      },
    });
  }
  // A dated version's page lives in R2. Pages can still answer its path with a copy from a
  // deployment that held it, so R2 is asked first.
  const page = PAGE.test(url.pathname);
  if (page) {
    const r = await fromR2(request, env, url, latest);
    if (r) {
      return r;
    }
  }
  const asset = await env.ASSETS.fetch(request);
  if (asset.status !== 404) {
    if (asset.status !== 200 && asset.status !== 206) {
      return asset;
    }
    // Pages _headers rules allow one splat, so the versioned tree gets its cache policy here.
    const dated = DATED.test(url.pathname) && !PAGE.test(url.pathname);
    const cd = disposition(decodeURIComponent(url.pathname.slice(1)), latest);
    if (!dated && !cd) {
      return asset;
    }
    const r = new Response(asset.body, asset);
    if (dated) {
      r.headers.set('cache-control', 'public, max-age=31536000, immutable, no-transform');
    }
    if (cd) {
      r.headers.set('content-disposition', cd);
    }
    return r;
  }
  return (!page && (await fromR2(request, env, url, latest))) || asset;
}

async function fromR2(request, env, url, latest) {
  const path = decodeURIComponent(url.pathname.replace(/^\//, ''));
  const key = path.endsWith('/') ? path + 'index.html' : path;
  const head = request.method === 'HEAD';
  const read = (bucket, k) =>
    head ? bucket.head(k) : bucket.get(k, { range: request.headers, onlyIf: request.headers });
  let obj = (await read(env.DIST, key)) || (await rawSource(env, url, key, read));
  // A new version's data.csv.gz is not stored apart: its data.csv is stored as those very bytes.
  let alias = false;
  if (!obj && key.endsWith('.csv.gz') && DATED.test(key)) {
    const csv = await read(env.DIST, key.slice(0, -3));
    if (csv && gzipped(csv)) {
      [obj, alias] = [csv, true];
    } else if (csv && csv.body) {
      await csv.body.cancel();
    }
  }
  // A text file is stored gzipped, so a range of its stored bytes means nothing to the client.
  const decoded = obj && gzipped(obj) && !alias;
  if (decoded && !head && obj.body && request.headers.has('range')) {
    await obj.body.cancel();
    obj = await env.DIST.get(key, { onlyIf: request.headers });
  }
  if (!obj) {
    // A version page asked for without its trailing slash, as Pages would redirect it.
    if (
      DATED.test(url.pathname + '/') &&
      !/\.[a-z0-9]+$/i.test(key) &&
      (await env.DIST.head(key + '/index.html'))
    ) {
      return new Response(null, {
        status: 308,
        headers: { location: url.pathname + '/' + url.search },
      });
    }
    return null;
  }
  return answer(request, url, key, obj, head, env, latest, decoded);
}

// An R2 object as a response: its type, size and byte range, and its cache policy by its key.
async function answer(request, url, key, obj, head, env, latest, decoded = gzipped(obj)) {
  // A revalidation that will be answered 304 needs no bytes, so it is not sent on.
  const sending = head || ('body' in obj && obj.body);
  if (
    sending &&
    obj.size > EDGE_MAX &&
    DATED.test(key) &&
    url.searchParams.get('edge') !== BYPASS
  ) {
    // A file that large goes to its own URL marked edge=bypass, which a zone Cache Rule keeps
    // out of the cache, so every range reaches R2 and comes back as a 206.
    if (obj.body) {
      await obj.body.cancel();
    }
    const to = new URL(url);
    to.searchParams.set('edge', BYPASS);
    return new Response(null, {
      status: 302,
      headers: {
        location: to.href,
        'cache-control': 'public, max-age=86400',
        'access-control-allow-origin': '*',
      },
    });
  }
  const headers = new Headers();
  obj.writeHttpMetadata(headers);
  headers.delete('content-encoding');
  const ext = key.split('.').pop();
  if (TYPES[ext]) {
    headers.set('content-type', TYPES[ext]);
  }
  if (key.endsWith('.csv-metadata.json')) {
    headers.set('content-type', 'application/csvm+json');
  }
  headers.set('etag', obj.httpEtag);
  headers.set('accept-ranges', decoded ? 'none' : 'bytes');
  headers.set('access-control-allow-origin', '*');
  // no-transform keeps the edge from compressing the body, which would drop the byte range. A
  // gzipped text file has no range to keep, and the edge must be free to decode it for a client
  // that cannot, since it caches whichever encoding it was sent first.
  const page = PAGE.test('/' + key);
  headers.set(
    'cache-control',
    DATED.test(key) && !page
      ? 'public, max-age=31536000, immutable, no-transform'
      : 'public, max-age=300, no-transform',
  );
  if (decoded) {
    headers.set('cache-control', headers.get('cache-control').replace(', no-transform', ''));
  }
  if (page) {
    for (const [k, v] of Object.entries(await headersFor(env))) {
      headers.set(k, v);
    }
  }
  const cd = disposition(key, latest);
  if (cd) {
    headers.set('content-disposition', cd);
  }
  if (decoded) {
    return textResponse(request, obj, headers, head);
  }
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
      if (obj.range.suffix !== undefined) {
        start = obj.size - obj.range.suffix;
        end = obj.size - 1;
      } else {
        start = obj.range.offset || 0;
        end = start + (obj.range.length || obj.size - start) - 1;
      }
      headers.set('content-range', `bytes ${start}-${end}/${obj.size}`);
    }
    return new Response(obj.body, { status, headers });
  }
  return new Response(null, { status: 304, headers });
}

// The Workers runtime always asks for gzip itself; what the client asked for is on cf.
function acceptsGzip(request) {
  const ae =
    (request.cf && request.cf.clientAcceptEncoding) ?? request.headers.get('accept-encoding') ?? '';
  return ae.split(',').some((t) => {
    const [coding, ...params] = t.trim().toLowerCase().split(';');
    const q = params.map((p) => p.trim()).find((p) => p.startsWith('q='));
    return (
      (coding === 'gzip' || coding === 'x-gzip' || coding === '*') &&
      !(q && Number(q.slice(2)) === 0)
    );
  });
}

// A stored gzipped text file goes out as stored to a client that takes gzip, and decoded
// otherwise. Either way the client ends up with the same bytes, at the size its metadata records.
function textResponse(request, obj, headers, head) {
  headers.append('vary', 'Accept-Encoding');
  const encoded = acceptsGzip(request);
  if (encoded) {
    headers.set('content-encoding', 'gzip');
  } else {
    headers.set('etag', 'W/' + obj.httpEtag);
  }
  const size = encoded ? obj.size : (obj.customMetadata || {}).size;
  if (head) {
    if (size !== undefined) {
      headers.set('content-length', String(size));
    }
    return new Response(null, { status: 200, headers });
  }
  if (!('body' in obj) || !obj.body) {
    return new Response(null, { status: 304, headers });
  }
  if (encoded) {
    return new Response(obj.body, { status: 200, headers, encodeBody: 'manual' });
  }
  return new Response(gunzip(obj.body).body, { status: 200, headers });
}

export const onRequestHead = onRequestGet;
