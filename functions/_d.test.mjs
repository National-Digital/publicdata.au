import assert from 'node:assert/strict';
import { test } from 'node:test';
import { onRequestGet } from './d/[[path]].js';
import { disposition } from './_download.js';

const R2 = {
  'd/x/v/2026-04-24/index.html': '<html>v</html>',
  'd/x/v/2026-04-24/index.md': '# v',
  'd/x/v/2026-04-24/data.csv': 'a,b\n1,2\n',
};
const BIG = 'd/x/v/2026-04-24/data.duckdb';
const obj = (key, body, range) => ({
  size: key === BIG ? 3e9 : body.length, httpEtag: '"e"', range,
  body: key === BIG && body ? new Blob([body]).stream() : body,
  writeHttpMetadata() {},
});
R2[BIG] = 'duck';
const env = {
  ASSETS: { fetch: async (r) => {
    const u = new URL(r.url || r);
    if (u.pathname === '/static/page-headers.json') return Response.json({ 'Content-Security-Policy': "default-src 'self'", 'X-Content-Type-Options': 'nosniff' });
    if (u.pathname === '/latest.json') return Response.json({ x: '2026-04-24' });
    if (u.pathname === '/withheld.json') return Response.json(['/d/x/v/2026-04-24/source.csv']);
    return new Response('nf', { status: 404 });
  } },
  DIST: {
    get: async (k, o) => (k in R2 ? obj(k, R2[k], o && o.range ? { offset: 0, length: R2[k].length } : undefined) : null),
    head: async (k) => (k in R2 ? obj(k, '', undefined) : null),
    list: async ({ prefix }) => ({ objects: Object.keys(R2).filter((k) => k.startsWith(prefix)).map((key) => ({ key })) }),
  },
};
const get = (path, headers = {}) => onRequestGet({ request: new Request('https://publicdata.au' + path, { headers }), env });

// First, because the headers are remembered once a lookup succeeds.
test('a failed header lookup is not remembered, and the page still goes out with nosniff', async (t) => {
  const logged = t.mock.method(console, 'error', () => {});
  let calls = 0;
  const flaky = { ...env, ASSETS: { fetch: async (r) => (new URL(r.url || r).pathname === '/static/page-headers.json' && ++calls === 1 ? new Response('no', { status: 500 }) : env.ASSETS.fetch(r)) } };
  const one = await onRequestGet({ request: new Request('https://publicdata.au/d/x/v/2026-04-24/'), env: flaky });
  assert.equal(one.headers.get('x-content-type-options'), 'nosniff');
  assert.equal(one.headers.get('content-security-policy'), null);
  assert.equal(logged.mock.callCount(), 1);
  const two = await onRequestGet({ request: new Request('https://publicdata.au/d/x/v/2026-04-24/'), env: flaky });
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
  const stale = { ...env, ASSETS: { fetch: async (r) => (new URL(r.url || r).pathname === '/d/x/v/2026-04-24/' ? new Response('<html>old</html>', { headers: { 'x-robots-tag': 'noindex' } }) : env.ASSETS.fetch(r)) } };
  const r = await onRequestGet({ request: new Request('https://publicdata.au/d/x/v/2026-04-24/'), env: stale });
  assert.equal(await r.text(), '<html>v</html>');
  assert.equal(r.headers.get('x-robots-tag'), null);
  const gone = { ...stale, DIST: { get: async () => null, head: async () => null } };
  const p = await onRequestGet({ request: new Request('https://publicdata.au/d/x/v/2026-04-24/'), env: gone });
  assert.equal(await p.text(), '<html>old</html>');
});

test('a file over the edge cache limit is sent to its bypass URL, which serves the range', async () => {
  for (const method of ['GET', 'HEAD']) {
    const r = await onRequestGet({ request: new Request('https://publicdata.au/d/x/v/2026-04-24/data.duckdb', { method, headers: { range: 'bytes=0-1' } }), env });
    assert.equal(r.status, 302);
    assert.equal(r.headers.get('location'), 'https://publicdata.au/d/x/v/2026-04-24/data.duckdb?edge=bypass');
  }
  const r = await get('/d/x/v/2026-04-24/data.duckdb?edge=bypass', { range: 'bytes=0-1' });
  assert.equal(r.status, 206);
  const h = await onRequestGet({ request: new Request('https://publicdata.au/d/x/v/2026-04-24/data.duckdb?edge=bypass', { method: 'HEAD' }), env });
  assert.equal(h.status, 200);
  assert.equal(h.headers.get('content-length'), '3000000000');
  assert.equal((await get('/d/x/v/2026-04-24/data.csv')).status, 200);
  const cond = { ...env, DIST: { ...env.DIST, get: async (k) => ({ ...obj(k, ''), body: undefined }) } };
  const reval = await onRequestGet({ request: new Request('https://publicdata.au/d/x/v/2026-04-24/data.duckdb', { headers: { 'if-none-match': '"e"' } }), env: cond });
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
  assert.equal((await get('/d/x/latest/SHA256SUMS')).headers.get('location'), '/d/x/v/2026-04-24/SHA256SUMS');
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
  const dist = { ...env.DIST, head: async (k) => (published.includes(k) ? obj(k, '') : env.DIST.head(k)) };
  // A preview holds its new version's manifest on Pages.
  const pages = { fetch: async (r) => (new URL(r.url || r).pathname === '/d/x/v/2026-06-01/manifest.json' ? new Response('{}') : env.ASSETS.fetch(r)) };
  const e = { ...env, ASSETS: pages, DIST: dist, RAW: raw };
  const at = (path, method = 'GET') => onRequestGet({ request: new Request('https://publicdata.au' + path, { method }), env: e });
  const r = await at('/d/x/v/2026-03-01/source.csv');
  assert.equal(r.status, 200);
  assert.equal(await r.text(), 'a,b\n1,2\n');
  assert.match(r.headers.get('cache-control'), /immutable/);
  assert.equal(r.headers.get('content-disposition'), disposition('d/x/v/2026-03-01/source.csv'));
  assert.equal((await at('/d/x/v/2026-03-01/source.csv', 'HEAD')).headers.get('content-length'), '8');
  assert.equal(await (await at('/d/x/v/2026-06-01/source.csv')).text(), 'preview');
  assert.equal((await at('/d/x/v/2026-05-01/source.csv')).status, 404);
  assert.equal((await at('/d/x/v/2026-03-01/source.zip')).status, 404);
  // A register entry that withholds its source still refuses the raw store's copy.
  assert.equal((await at('/d/x/v/2026-04-24/source.csv')).status, 410);
});

test('a path with escapes is sent to its plain form, where a withheld file is still refused', async () => {
  for (const path of ['/d/x/v/2026-04-24/sourc%65.csv', '/d/x/v/2026-04-24/source%2Ecsv', '/d/%78/v/2026-04-24/source.csv']) {
    const r = await get(path);
    assert.equal(r.status, 308, path);
    assert.equal(r.headers.get('location'), '/d/x/v/2026-04-24/source.csv');
    assert.equal((await get(r.headers.get('location'))).status, 410);
  }
  assert.equal((await get('/d/%67one/v/2026-04-24/data.csv')).headers.get('location'), '/d/gone/v/2026-04-24/data.csv');
  assert.equal((await get('/d/x/v/2026-04-24/data.csv%3Fa')).status, 404);
  assert.equal((await get('/d/x/v/2026-04-24/%E0%A4%A')).status, 404);
  assert.equal((await get('/d/x/v/2026-04-24/data.csv')).status, 200);
});
