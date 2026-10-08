import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { test } from 'node:test';
import { answer, versions } from './_api.js';
import { rowsQuery } from './_query.js';

// A stand-in for the D1 binding over node:sqlite, and an in-memory edge cache.
function d1(db) {
  return {
    prepare(sql) {
      let args = [];
      const stmt = {
        bind(...a) {
          args = a;
          return stmt;
        },
        async first() {
          const r = db.prepare(sql).get(...args);
          return r ? { ...r } : null;
        },
        async all() {
          return {
            results: db
              .prepare(sql)
              .all(...args)
              .map((r) => ({ ...r })),
          };
        },
      };
      return stmt;
    },
  };
}
const store = new Map();
globalThis.caches = {
  default: {
    match: async (req) => store.get(req.url)?.clone(),
    put: async (req, res) => {
      store.set(req.url, res);
    },
  },
};

const db = new DatabaseSync(':memory:');
db.exec(`CREATE TABLE "v_t_20260424" (lga TEXT, year INTEGER);
CREATE TABLE _versions (slug TEXT, version TEXT, tbl TEXT, fields TEXT, rows INTEGER, attribution TEXT, header TEXT);`);
const ins = db.prepare('INSERT INTO "v_t_20260424" VALUES (?, ?)');
for (let i = 0; i < 5; i++) ins.run(`LGA ${i}`, 2020 + i);
const header = {
  dataset: 'test-data',
  version: '2026-04-24',
  publisher: { name: 'Test Agency' },
  licence: { id: 'CC-BY-4.0' },
  attribution: 'Test Agency, Test, CC BY 4.0.',
  source: {
    url: 'https://example.gov.au/f.csv',
    fetched_at: '2026-04-24T00:00:00Z',
    sha256: 'abc123',
  },
};
db.prepare('INSERT INTO _versions VALUES (?, ?, ?, ?, ?, ?, ?)').run(
  'test-data',
  '2026-04-24',
  'v_t_20260424',
  JSON.stringify([
    { name: 'lga', type: 'string' },
    { name: 'year', type: 'integer' },
  ]),
  5,
  header.attribution,
  JSON.stringify(header),
);

const call = async (qs, env = { DB: d1(db) }, version) => {
  const waits = [];
  const path = version ? `test-data/versions/${version}/rows` : 'test-data/rows';
  const params = version ? { slug: 'test-data', version } : { slug: 'test-data' };
  const res = await answer(
    {
      request: new Request(`https://publicdata.au/api/v1/datasets/${path}${qs}`),
      env,
      params,
      waitUntil: (p) => waits.push(p),
    },
    rowsQuery,
    'rows',
  );
  await Promise.all(waits);
  return res;
};

test('without a D1 binding the API says so and points to the files', async () => {
  const r = await call('', {});
  assert.equal(r.status, 503);
  assert.match((await r.json()).error, /not enabled/);
});

test('a bound database that was never loaded reads as not enabled', async () => {
  const empty = new DatabaseSync(':memory:');
  const r = await call('', { DB: d1(empty) });
  assert.equal(r.status, 503);
  assert.match((await r.json()).error, /not enabled/);
});

test('JSON carries the whole provenance header and CSV carries it in headers', async () => {
  const r = await call('?limit=2');
  const body = await r.json();
  assert.equal(r.status, 200);
  assert.deepEqual(body.publicdata, header);
  assert.equal(body.rows.length, 2);
  assert.equal(
    body.next,
    'https://publicdata.au/api/v1/datasets/test-data/versions/2026-04-24/rows?limit=2&offset=2',
  );
  const c = await call('?limit=2&format=csv');
  assert.equal(c.headers.get('x-publicdata-source-sha256'), 'abc123');
  assert.equal(decodeURIComponent(c.headers.get('x-publicdata-attribution')), header.attribution);
  assert.match(c.headers.get('link'), /manifest\.json>; rel="describedby"/);
  assert.equal(c.headers.get('ratelimit-policy'), '"fair-use";q=60;w=10');
  assert.match(c.headers.get('access-control-expose-headers'), /X-Publicdata-Attribution/);
  assert.equal((await call('?nope=eq.1')).headers.get('ratelimit-policy'), '"fair-use";q=60;w=10');
  assert.equal(await c.text(), 'lga,year\nLGA 0,2020\nLGA 1,2021\n');
});

test('pages follow each other without a gap or a repeat', async () => {
  const seen = [];
  let url = '?limit=2';
  for (let i = 0; i < 5 && url; i++) {
    const body = await (await call(url)).json();
    seen.push(...body.rows.map((r) => r.lga));
    url = body.next ? new URL(body.next).search : null;
  }
  assert.deepEqual(seen, ['LGA 0', 'LGA 1', 'LGA 2', 'LGA 3', 'LGA 4']);
});

test('bad queries and unknown versions are explained', async () => {
  assert.equal((await call('?nope=eq.1')).status, 400);
  const v = await call('', undefined, '2020-01-01');
  assert.equal(v.status, 404);
  const vb = await v.json();
  assert.match(vb.error, /loaded versions are 2026-04-24/);
  assert.equal(vb.versions, 'https://publicdata.au/api/v1/datasets/test-data/versions');
  assert.equal((await call('', undefined, 'yesterday')).status, 404);
  assert.equal((await call('?format=xml')).status, 400);
});

test('a named version is cached for good and served from the cache', async () => {
  const first = await call('?limit=1', undefined, '2026-04-24');
  assert.match(first.headers.get('cache-control'), /immutable/);
  db.exec('UPDATE "v_t_20260424" SET lga = \'changed\'');
  const again = await (await call('?limit=1', undefined, '2026-04-24')).json();
  assert.equal(again.rows[0].lga, 'LGA 0');
});

const listVersions = async (slug, env = { DB: d1(db) }) => {
  const waits = [];
  const res = await versions({
    request: new Request(`https://publicdata.au/api/v1/datasets/${slug}/versions`),
    env,
    params: { slug },
    waitUntil: (p) => waits.push(p),
  });
  await Promise.all(waits);
  return res;
};

test('versions lists what is loaded with the URLs to query each', async () => {
  const r = await listVersions('test-data');
  const body = await r.json();
  assert.equal(body.versions[0].version, '2026-04-24');
  assert.equal(
    body.versions[0].rows_url,
    'https://publicdata.au/api/v1/datasets/test-data/versions/2026-04-24/rows',
  );
  assert.ok(store.has('https://publicdata.au/api/v1/datasets/test-data/versions'));
  assert.equal((await listVersions('test-data', {})).status, 503);
  assert.equal((await listVersions('nothing-here')).status, 404);
});

test('an API path nothing answers is a JSON 404 that points to the docs', async () => {
  const { onRequest } = await import('./api/[[path]].js');
  const r = onRequest();
  assert.equal(r.status, 404);
  const body = await r.json();
  assert.equal(body.docs, 'https://publicdata.au/agents/#query-api');
  assert.equal(body.openapi, 'https://publicdata.au/openapi.json');
});

test('a dataset the site no longer lists as live is refused though its tables are loaded', async () => {
  const ASSETS = { fetch: async () => Response.json({ other: '2026-04-24' }) };
  const r = await call('', { DB: d1(db), ASSETS });
  assert.equal(r.status, 410);
  assert.match((await r.json()).error, /withheld/);
  assert.equal((await listVersions('test-data', { DB: d1(db), ASSETS })).status, 410);
});
