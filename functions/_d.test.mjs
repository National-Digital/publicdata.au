import assert from 'node:assert/strict';
import { test } from 'node:test';
import { onRequestGet } from './d/[[path]].js';
import { disposition } from './_download.js';

const R2 = {
  'd/x/v/2026-04-24/index.html': '<html>v</html>',
  'd/x/v/2026-04-24/index.md': '# v',
  'd/x/v/2026-04-24/data.csv': 'a,b\n1,2\n',
  'd/r/fetch/2026-10-06/data.parquet': 'PAR1',
  // A file an older fetch wrote, which latest/ must never serve again.
  'd/r/fetch/2026-09-29/data.xlsx': 'old',
  'd/r/latest/data.xlsx': 'old',
};
const BIG = 'd/x/v/2026-04-24/data.duckdb';
const obj = (key, body, range) => ({
  size: key === BIG ? 3e9 : body.length,
  httpEtag: '"e"',
  range,
  body: key === BIG && body ? new Blob([body]).stream() : body,
  writeHttpMetadata() {},
});
R2[BIG] = 'duck';
const env = {
  ASSETS: {
    fetch: async (r) => {
      const u = new URL(r.url || r);
      if (u.pathname === '/static/page-headers.json') {
        return Response.json({
          'Content-Security-Policy': "default-src 'self'",
          'X-Content-Type-Options': 'nosniff',
        });
      }
      if (u.pathname === '/latest.json') {
        return Response.json({ x: '2026-04-24', r: '2026-09-01' });
      }
      if (u.pathname === '/current.json') {
        return Response.json({ r: { fetch: '2026-10-06', source: 'source.csv' } });
      }
      if (u.pathname === '/d/r/fetch/2026-10-06/data.csv') {
        return new Response('k,v\n1,2\n', {
          headers: { 'cache-control': 'public, max-age=31536000' },
        });
      }
      if (u.pathname === '/withheld.json') {
        return Response.json(['/d/x/v/2026-04-24/source.csv']);
      }
      return new Response('nf', { status: 404 });
    },
  },
  DIST: {
    get: async (k, o) =>
      k in R2
        ? obj(k, R2[k], o && o.range ? { offset: 0, length: R2[k].length } : undefined)
        : null,
    head: async (k) => (k in R2 ? obj(k, '', undefined) : null),
    list: async ({ prefix }) => ({
      objects: Object.keys(R2)
        .filter((k) => k.startsWith(prefix))
        .map((key) => ({ key })),
    }),
  },
};
const get = (path, headers = {}) =>
  onRequestGet({ request: new Request('https://publicdata.au' + path, { headers }), env });

// First, because the headers are remembered once a lookup succeeds.
test('a failed header lookup is not remembered, and the page still goes out with nosniff', async (t) => {
  const logged = t.mock.method(console, 'error', () => {});
  let calls = 0;
  const flaky = {
    ...env,
    ASSETS: {
      fetch: async (r) =>
        new URL(r.url || r).pathname === '/static/page-headers.json' && ++calls === 1
          ? new Response('no', { status: 500 })
          : env.ASSETS.fetch(r),
    },
  };
  const one = await onRequestGet({
    request: new Request('https://publicdata.au/d/x/v/2026-04-24/'),
    env: flaky,
  });
  assert.equal(one.headers.get('x-content-type-options'), 'nosniff');
  assert.equal(one.headers.get('content-security-policy'), null);
  assert.equal(logged.mock.callCount(), 1);
  const two = await onRequestGet({
    request: new Request('https://publicdata.au/d/x/v/2026-04-24/'),
    env: flaky,
  });
  assert.equal(two.headers.get('content-security-policy'), "default-src 'self'");
});

test('a version page read from R2 is HTML with the site headers and a short cache', async () => {
  const r = await get('/d/x/v/2026-04-24/');
  assert.equal(r.status, 200);
  assert.equal(r.headers.get('content-type'), 'text/html; charset=utf-8');
  assert.equal(r.headers.get('content-security-policy'), "default-src 'self'");
  assert.match(r.headers.get('cache-control'), /max-age=300/);
  assert.equal(await r.text(), '<html>v</html>');
  const md = await get('/d/x/v/2026-04-24/index.md');
  assert.equal(md.headers.get('content-type'), 'text/markdown; charset=utf-8');
});

test('a version page without its slash redirects, and a file keeps its long cache', async () => {
  const r = await get('/d/x/v/2026-04-24');
  assert.equal(r.status, 308);
  assert.equal(r.headers.get('location'), '/d/x/v/2026-04-24/');
  const f = await get('/d/x/v/2026-04-24/data.csv');
  assert.equal(f.status, 200);
  assert.match(f.headers.get('cache-control'), /immutable/);
  assert.equal(f.headers.get('content-security-policy'), null);
  assert.equal((await get('/d/x/v/2026-04-24/data.csv', { range: 'bytes=0-2' })).status, 206);
  assert.equal((await get('/d/x/v/2026-04-24/nothing.csv')).status, 404);
});

test('a version page comes from R2 even when Pages still answers its path', async () => {
  const stale = {
    ...env,
    ASSETS: {
      fetch: async (r) =>
        new URL(r.url || r).pathname === '/d/x/v/2026-04-24/'
          ? new Response('<html>old</html>', { headers: { 'x-robots-tag': 'noindex' } })
          : env.ASSETS.fetch(r),
    },
  };
  const r = await onRequestGet({
    request: new Request('https://publicdata.au/d/x/v/2026-04-24/'),
    env: stale,
  });
  assert.equal(await r.text(), '<html>v</html>');
  assert.equal(r.headers.get('x-robots-tag'), null);
  const gone = { ...stale, DIST: { get: async () => null, head: async () => null } };
  const p = await onRequestGet({
    request: new Request('https://publicdata.au/d/x/v/2026-04-24/'),
    env: gone,
  });
  assert.equal(await p.text(), '<html>old</html>');
});

test('a file over the edge cache limit is sent to its bypass URL, which serves the range', async () => {
  for (const method of ['GET', 'HEAD']) {
    const r = await onRequestGet({
      request: new Request('https://publicdata.au/d/x/v/2026-04-24/data.duckdb', {
        method,
        headers: { range: 'bytes=0-1' },
      }),
      env,
    });
    assert.equal(r.status, 302);
    assert.equal(
      r.headers.get('location'),
      'https://publicdata.au/d/x/v/2026-04-24/data.duckdb?edge=bypass',
    );
  }
  const r = await get('/d/x/v/2026-04-24/data.duckdb?edge=bypass', { range: 'bytes=0-1' });
  assert.equal(r.status, 206);
  const h = await onRequestGet({
    request: new Request('https://publicdata.au/d/x/v/2026-04-24/data.duckdb?edge=bypass', {
      method: 'HEAD',
    }),
    env,
  });
  assert.equal(h.status, 200);
  assert.equal(h.headers.get('content-length'), '3000000000');
  assert.equal((await get('/d/x/v/2026-04-24/data.csv')).status, 200);
  const cond = {
    ...env,
    DIST: { ...env.DIST, get: async (k) => ({ ...obj(k, ''), body: undefined }) },
  };
  const reval = await onRequestGet({
    request: new Request('https://publicdata.au/d/x/v/2026-04-24/data.duckdb', {
      headers: { 'if-none-match': '"e"' },
    }),
    env: cond,
  });
  assert.equal(reval.status, 304);
});

test('a dataset that is not live has its archived files refused', async () => {
  R2['d/gone/v/2026-04-24/data.csv'] = 'a\n1\n';
  const r = await get('/d/gone/v/2026-04-24/data.csv');
  assert.equal(r.status, 410);
  assert.match(await r.text(), /withheld/);
  assert.equal((await get('/d/gone/v/2026-04-24/nothing.csv')).status, 404);
  assert.equal((await get('/d/gone/latest/data.csv')).status, 410);
  assert.equal((await get('/d/never/latest/data.csv')).status, 404);
  assert.equal((await get('/d/x/v/2026-04-24/data.csv')).status, 200);
});

test('a publisher file the register no longer republishes is refused', async () => {
  R2['d/x/v/2026-04-24/source.csv'] = 'a,icsea\n1,1000\n';
  assert.equal((await get('/d/x/v/2026-04-24/source.csv')).status, 410);
  assert.equal((await get('/d/x/v/2026-04-24/data.csv')).status, 200);
});

test('a dated file saves under its dataset and version, and a page has no file name', async () => {
  const r = await get('/d/x/v/2026-04-24/data.csv');
  assert.equal(r.headers.get('content-disposition'), 'inline; filename="x_2026-04-24.csv"');
  const ranged = await get('/d/x/v/2026-04-24/data.csv', { range: 'bytes=0-1' });
  assert.equal(ranged.headers.get('content-disposition'), 'inline; filename="x_2026-04-24.csv"');
  assert.equal((await get('/d/x/v/2026-04-24/')).headers.get('content-disposition'), null);
  assert.equal((await get('/d/x/v/2026-04-24/index.md')).headers.get('content-disposition'), null);
});

test("a version's checksum list is part of the version: cached for a year as immutable, with no file name", async () => {
  R2['d/x/v/2026-04-24/SHA256SUMS'] = `${'0'.repeat(64)}  x_2026-04-24.csv\n`;
  const r = await get('/d/x/v/2026-04-24/SHA256SUMS');
  assert.equal(r.status, 200);
  assert.equal(r.headers.get('cache-control'), 'public, max-age=31536000, immutable, no-transform');
  assert.equal(r.headers.get('content-disposition'), null);
  assert.equal(
    (await get('/d/x/latest/SHA256SUMS')).headers.get('location'),
    '/d/x/v/2026-04-24/SHA256SUMS',
  );
});

test("a version's source is the raw store's copy, and only for a version that was published", async () => {
  const RAW = {
    'x/2026-04-24/source.csv': 'withheld',
    'x/2026-03-01/source.csv': 'a,b\n1,2\n',
    'x/2026-05-01/source.csv': 'never published',
    'x/2026-06-01/source.csv': 'preview',
  };
  const raw = {
    get: async (k) => (k in RAW ? obj(k, RAW[k]) : null),
    head: async (k) => (k in RAW ? obj(k, RAW[k]) : null),
  };
  const published = ['d/x/v/2026-04-24/manifest.json', 'd/x/v/2026-03-01/manifest.json'];
  const dist = {
    ...env.DIST,
    head: async (k) => (published.includes(k) ? obj(k, '') : env.DIST.head(k)),
  };
  // A preview holds its new version's manifest on Pages.
  const pages = {
    fetch: async (r) =>
      new URL(r.url || r).pathname === '/d/x/v/2026-06-01/manifest.json'
        ? new Response('{}')
        : env.ASSETS.fetch(r),
  };
  const e = { ...env, ASSETS: pages, DIST: dist, RAW: raw };
  const at = (path, method = 'GET') =>
    onRequestGet({ request: new Request('https://publicdata.au' + path, { method }), env: e });
  const r = await at('/d/x/v/2026-03-01/source.csv');
  assert.equal(r.status, 200);
  assert.equal(await r.text(), 'a,b\n1,2\n');
  assert.match(r.headers.get('cache-control'), /immutable/);
  assert.equal(r.headers.get('content-disposition'), disposition('d/x/v/2026-03-01/source.csv'));
  assert.equal(
    (await at('/d/x/v/2026-03-01/source.csv', 'HEAD')).headers.get('content-length'),
    '8',
  );
  assert.equal(await (await at('/d/x/v/2026-06-01/source.csv')).text(), 'preview');
  assert.equal((await at('/d/x/v/2026-05-01/source.csv')).status, 404);
  assert.equal((await at('/d/x/v/2026-03-01/source.zip')).status, 404);
  // A register entry that withholds its source still refuses the raw store's copy.
  assert.equal((await at('/d/x/v/2026-04-24/source.csv')).status, 410);
});

test('a path with escapes is sent to its plain form, where a withheld file is still refused', async () => {
  for (const path of [
    '/d/x/v/2026-04-24/sourc%65.csv',
    '/d/x/v/2026-04-24/source%2Ecsv',
    '/d/%78/v/2026-04-24/source.csv',
  ]) {
    const r = await get(path);
    assert.equal(r.status, 308, path);
    assert.equal(r.headers.get('location'), '/d/x/v/2026-04-24/source.csv');
    assert.equal((await get(r.headers.get('location'))).status, 410);
  }
  assert.equal(
    (await get('/d/%67one/v/2026-04-24/data.csv')).headers.get('location'),
    '/d/gone/v/2026-04-24/data.csv',
  );
  assert.equal((await get('/d/x/v/2026-04-24/data.csv%3Fa')).status, 404);
  assert.equal((await get('/d/x/v/2026-04-24/%E0%A4%A')).status, 404);
  assert.equal((await get('/d/x/v/2026-04-24/data.csv')).status, 200);
});

test("a rolling source's latest/ is its newest fetch, served in place with a five-minute cache", async () => {
  const pages = await get('/d/r/latest/data.csv');
  assert.equal(pages.status, 200);
  assert.equal(await pages.text(), 'k,v\n1,2\n');
  assert.equal(pages.headers.get('cache-control'), 'public, max-age=300, no-transform');
  assert.equal(pages.headers.get('location'), null);
  assert.equal(pages.headers.get('content-disposition'), 'inline; filename="r_2026-10-06.csv"');
  const r2 = await get('/d/r/latest/data.parquet');
  assert.equal(r2.status, 200);
  assert.equal(await r2.text(), 'PAR1');
  assert.match(r2.headers.get('cache-control'), /max-age=300/);
  // Only the current fetch's folder is read, so an older fetch's file is gone from latest/.
  assert.equal((await get('/d/r/latest/data.xlsx')).status, 404);
  const raw = {
    get: async (k) => (k === 'r/2026-10-06/source.csv' ? obj(k, 'src') : null),
    head: async () => null,
  };
  const src = await onRequestGet({
    request: new Request('https://publicdata.au/d/r/latest/source.csv'),
    env: { ...env, RAW: raw },
  });
  assert.equal(src.status, 200);
  assert.equal(await src.text(), 'src');
  assert.equal(
    src.headers.get('content-disposition'),
    'inline; filename="r_2026-10-06_source.csv"',
  );
  // A release still redirects to its newest dated version.
  const rel = await get('/d/x/latest/data.csv');
  assert.equal(rel.status, 302);
  assert.equal(rel.headers.get('location'), '/d/x/v/2026-04-24/data.csv');
});

// A text file as dist-push stores it: gzipped, marked so, with its decoded size and hash.
const { gzipSync } = await import('node:zlib');
const CSV = 'a,b\n' + '1,2\n'.repeat(500);
const STORED = {
  'd/x/v/2026-04-24/data.csv': gzipSync(CSV),
  'd/x/v/2026-04-24/data.json': gzipSync('{"records":[]}'),
  'd/x/v/2026-04-24/data.parquet': Buffer.from('PAR1....PAR1'),
  'd/x/v/2026-03-01/data.csv': Buffer.from(CSV),
  'd/x/v/2026-04-24/data.ndjson': gzipSync('{"a":1}\n'.repeat(200)),
};
const stored = (k, opts = {}, withBody = true) => {
  const bytes = STORED[k];
  const gz = bytes[0] === 0x1f;
  const range =
    opts.range && opts.range.has && opts.range.has('range')
      ? (() => {
          const [, a, b] = opts.range.get('range').match(/bytes=(\d+)-(\d+)/);
          return { offset: Number(a), length: Number(b) - Number(a) + 1 };
        })()
      : undefined;
  const body = range ? bytes.subarray(range.offset, range.offset + range.length) : bytes;
  return {
    size: bytes.length,
    httpEtag: '"g"',
    range: opts.range ? range || { offset: 0, length: bytes.length } : undefined,
    httpMetadata: gz
      ? {
          contentEncoding: k.endsWith('.ndjson') ? 'gzip,aws-chunked' : 'gzip',
          contentType: 'text/csv; charset=utf-8',
        }
      : {},
    customMetadata: gz ? { size: String(k.endsWith('.csv') ? CSV.length : 14), sha256: 'x' } : {},
    ...(withBody ? { body: new Blob([body]).stream() } : {}),
    writeHttpMetadata(h) {
      if (gz) {
        h.set('content-encoding', 'gzip');
        h.set('content-type', 'text/csv; charset=utf-8');
      }
    },
  };
};
const reads = [];
const genv = {
  ...env,
  DIST: {
    get: async (k, o) => {
      reads.push([k, !!(o && o.range && o.range.has('range'))]);
      return k in STORED ? stored(k, o) : null;
    },
    head: async (k) => (k in STORED ? stored(k, {}, false) : null),
    list: env.DIST.list,
  },
};
const gget = (path, headers = {}, method = 'GET') =>
  onRequestGet({
    request: new Request('https://publicdata.au' + path, { method, headers }),
    env: genv,
  });

test('a gzipped text file goes out as stored to a client that takes gzip', async () => {
  const r = await gget('/d/x/v/2026-04-24/data.csv', { 'accept-encoding': 'gzip, br' });
  assert.equal(r.status, 200);
  assert.equal(r.headers.get('content-encoding'), 'gzip');
  assert.equal(r.headers.get('content-type'), 'text/csv; charset=utf-8');
  assert.equal(r.headers.get('vary'), 'Accept-Encoding');
  assert.equal(r.headers.get('accept-ranges'), 'none');
  assert.doesNotMatch(r.headers.get('cache-control'), /no-transform/);
  assert.match(r.headers.get('cache-control'), /immutable/);
  assert.deepEqual(Buffer.from(await r.arrayBuffer()), STORED['d/x/v/2026-04-24/data.csv']);
});

test('a gzipped text file is decoded for a client that does not take gzip', async () => {
  for (const ae of [undefined, 'identity', 'gzip;q=0, deflate']) {
    const r = await gget('/d/x/v/2026-04-24/data.csv', ae ? { 'accept-encoding': ae } : {});
    assert.equal(r.status, 200);
    assert.equal(r.headers.get('content-encoding'), null);
    assert.equal(r.headers.get('etag'), 'W/"g"');
    assert.equal(await r.text(), CSV);
  }
});

test('what the client asked for is read from cf, since the runtime always asks for gzip', async () => {
  const request = new Request('https://publicdata.au/d/x/v/2026-04-24/data.csv', {
    headers: { 'accept-encoding': 'br, gzip' },
  });
  Object.defineProperty(request, 'cf', { value: { clientAcceptEncoding: '' } });
  const r = await onRequestGet({ request, env: genv });
  assert.equal(r.headers.get('content-encoding'), null);
  assert.equal(await r.text(), CSV);
});

test('HEAD on a gzipped text file gives the size of what GET would send', async () => {
  const plain = await gget('/d/x/v/2026-04-24/data.csv', {}, 'HEAD');
  assert.equal(plain.status, 200);
  assert.equal(plain.headers.get('content-length'), String(CSV.length));
  assert.equal(plain.headers.get('content-encoding'), null);
  const enc = await gget('/d/x/v/2026-04-24/data.csv', { 'accept-encoding': 'gzip' }, 'HEAD');
  assert.equal(
    enc.headers.get('content-length'),
    String(STORED['d/x/v/2026-04-24/data.csv'].length),
  );
  assert.equal(enc.headers.get('content-encoding'), 'gzip');
});

test('a range on a gzipped text file is answered with the whole file', async () => {
  reads.length = 0;
  const r = await gget('/d/x/v/2026-04-24/data.csv', { range: 'bytes=0-9' });
  assert.equal(r.status, 200);
  assert.equal(r.headers.get('content-range'), null);
  assert.equal(r.headers.get('accept-ranges'), 'none');
  assert.equal(await r.text(), CSV);
  assert.deepEqual(reads.at(-1), ['d/x/v/2026-04-24/data.csv', false]);
  const p = await gget('/d/x/v/2026-04-24/data.parquet', { range: 'bytes=0-3' });
  assert.equal(p.status, 206);
  assert.equal(p.headers.get('accept-ranges'), 'bytes');
  assert.equal(await p.text(), 'PAR1');
});

test('data.csv.gz is served from the stored CSV as a gzip file, ranges and all', async () => {
  const gz = STORED['d/x/v/2026-04-24/data.csv'];
  const r = await gget('/d/x/v/2026-04-24/data.csv.gz', { 'accept-encoding': 'gzip' });
  assert.equal(r.status, 200);
  assert.equal(r.headers.get('content-type'), 'application/gzip');
  assert.equal(r.headers.get('content-encoding'), null);
  assert.match(r.headers.get('cache-control'), /no-transform/);
  assert.deepEqual(Buffer.from(await r.arrayBuffer()), gz);
  const part = await gget('/d/x/v/2026-04-24/data.csv.gz', { range: 'bytes=0-1' });
  assert.equal(part.status, 206);
  assert.equal(part.headers.get('content-range'), `bytes 0-1/${gz.length}`);
  assert.deepEqual(Buffer.from(await part.arrayBuffer()), gz.subarray(0, 2));
  const h = await gget('/d/x/v/2026-04-24/data.csv.gz', {}, 'HEAD');
  assert.equal(h.headers.get('content-length'), String(gz.length));
  // A CSV stored before gzip at rest has no gzip to alias.
  assert.equal((await gget('/d/x/v/2026-03-01/data.csv.gz')).status, 404);
  assert.equal(
    await (await gget('/d/x/v/2026-03-01/data.csv', { 'accept-encoding': 'gzip' })).text(),
    CSV,
  );
});

test('a stored encoding listed with aws-chunked is still gzip, and goes out as plain gzip', async () => {
  const enc = await gget('/d/x/v/2026-04-24/data.ndjson', { 'accept-encoding': 'gzip' });
  assert.equal(enc.headers.get('content-encoding'), 'gzip');
  assert.equal(enc.headers.get('content-type'), 'application/x-ndjson');
  const plain = await gget('/d/x/v/2026-04-24/data.ndjson');
  assert.equal(plain.headers.get('content-encoding'), null);
  assert.equal(await plain.text(), '{"a":1}\n'.repeat(200));
});

test('a part, a change log, the history archive and a fetch save under names that say what they are', async () => {
  R2['d/r/v/2026-09-01/parts/2022.parquet'] = 'PAR1';
  R2['d/r/v/2026-09-01/parts/undated.csv.gz'] = 'gz';
  R2['d/r/v/2026-09-01/history/2022.parquet'] = 'PAR1';
  R2['d/r/fetch/2026-10-06/parts/2026.parquet'] = 'PAR1';
  R2['d/r/history.tar.zst'] = 'zst';
  const PAGES = {
    '/d/r/changes/2026-10-06.json': '{}',
    '/d/r/changes/index.json': '[]',
    '/d/r/index.html': '<html>r</html>',
  };
  const pages = {
    fetch: async (r) => {
      const p = new URL(r.url || r).pathname;
      return p in PAGES
        ? new Response(PAGES[p], { headers: { 'cache-control': 'public, max-age=300' } })
        : env.ASSETS.fetch(r);
    },
  };
  const e = { ...env, ASSETS: pages };
  const name = async (path) =>
    (
      await onRequestGet({ request: new Request('https://publicdata.au' + path), env: e })
    ).headers.get('content-disposition');
  assert.equal(
    await name('/d/r/v/2026-09-01/parts/2022.parquet'),
    'inline; filename="r_2026-09-01_2022.parquet"',
  );
  assert.equal(
    await name('/d/r/v/2026-09-01/parts/undated.csv.gz'),
    'inline; filename="r_2026-09-01_undated.csv.gz"',
  );
  assert.equal(
    await name('/d/r/v/2026-09-01/history/2022.parquet'),
    'inline; filename="r_2026-09-01_history_2022.parquet"',
  );
  assert.equal(
    await name('/d/r/latest/parts/2026.parquet'),
    'inline; filename="r_2026-10-06_2026.parquet"',
  );
  assert.equal(
    await name('/d/r/fetch/2026-10-06/data.parquet'),
    'inline; filename="r_2026-10-06.parquet"',
  );
  assert.equal(await name('/d/r/fetch/2026-10-06/data.csv'), 'inline; filename="r_2026-10-06.csv"');
  assert.equal(
    await name('/d/r/changes/2026-10-06.json'),
    'inline; filename="r_2026-10-06_changes.json"',
  );
  assert.equal(await name('/d/r/changes/index.json'), 'inline; filename="r_changes_index.json"');
  // The archive is named for the newest version it holds, the one latest.json names.
  assert.equal(
    await name('/d/r/history.tar.zst'),
    'inline; filename="r_2026-09-01_history.tar.zst"',
  );
  assert.equal(await name('/d/r/'), null);
  assert.equal(await name('/d/r/changes/'), null);
  // Naming a change log leaves its cache as Pages set it.
  const log = await onRequestGet({
    request: new Request('https://publicdata.au/d/r/changes/index.json'),
    env: e,
  });
  assert.equal(log.headers.get('cache-control'), 'public, max-age=300');
});
