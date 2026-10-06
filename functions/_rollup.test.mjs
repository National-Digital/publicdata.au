import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { test } from 'node:test';
import { gunzipSync } from 'node:zlib';
import { aggregateQuery } from './_query.js';
import { aggregate, glob, rollup } from './_rollup.js';

// The rollup and the query API must give one answer. Both read the fixture the Python tests pin
// (pipeline/tests/fixtures/rollup): the rows go into node:sqlite and run through the API's own
// SQL, and the rollup built from them answers the same queries.
const FIX = new URL('../pipeline/tests/fixtures/rollup/', import.meta.url);
const data = JSON.parse(readFileSync(new URL('rows.json', FIX), 'utf8'));
const gz = readFileSync(new URL('rollup.json.gz', FIX));
const R = () => JSON.parse(gunzipSync(gz).toString('utf8'));

const SQL_TYPES = { string: 'TEXT', integer: 'INTEGER', boolean: 'INTEGER', date: 'TEXT' };
const db = new DatabaseSync(':memory:');
db.exec(`CREATE TABLE t (${data.fields.map((f) => `"${f.name}" ${SQL_TYPES[f.type]}`).join(', ')})`);
const ins = db.prepare(`INSERT INTO t VALUES (${data.fields.map(() => '?').join(', ')})`);
for (const r of data.rows) ins.run(...r);
const viaSQL = (qs) => {
  const plan = aggregateQuery('t', data.fields, new URLSearchParams(qs));
  const rows = db.prepare(plan.sql).all(...plan.binds).map((r) => ({ ...r }));
  const more = rows.length > plan.limit;
  return { rows: more ? rows.slice(0, plan.limit) : rows, more };
};
const count = (where) => viaSQL(['metric=count', ...where].join('&')).rows[0].count;

// Seeded, so a failure names a query that fails again.
let seed = 42;
const rand = (n) => { seed = (seed * 1103515245 + 12345) % 2 ** 31; return seed % n; };
const pick = (a) => a[rand(a.length)];
const vals = {
  lga: ['Gold Coast', 'brisbane', 'Brisbane', "O'Connor", 'Zürich', '𝔄stral', '100%_rural', 'Nowhere'],
  year: ['2019', '2020', '2021', '2022', '1999'],
  severity: ['Fatal', 'Minor', 'Hospitalisation', 'x'],
  fatal: ['true', 'false'],
  day: ['2021-01-05', '2021-06-30', '2022-02-01', '2021-03-01'],
};
const like = ['*coast*', 'BRIS*', '*%_*', 'z*', '*', '𝔄*', 'gold_coast'];
function filter() {
  const f = pick(Object.keys(vals));
  const not = rand(5) === 0 ? 'not.' : '';
  switch (rand(6)) {
    case 0: return `${f}=${not}is.null`;
    case 1: return `${f}=${not}in.(${[pick(vals[f]), pick(vals[f])].join(',')})`;
    case 2: return f === 'lga' ? `lga=${not}${pick(['like', 'ilike'])}.${pick(like)}` : `${f}=${not}eq.${pick(vals[f])}`;
    default: return `${f}=${not}${pick(['eq', 'neq', 'gt', 'gte', 'lt', 'lte'])}.${pick(vals[f])}`;
  }
}
const METRICS = ['count', 'sum.casualties', 'avg.casualties', 'min.speed', 'max.speed', 'count.speed', 'sum.fatal', 'min.casualties'];

test('the rollup answers every query it takes exactly as the query API does', () => {
  const r = R();
  let taken = 0;
  for (let i = 0; i < 2000; i++) {
    const group = [...new Set(Array.from({ length: rand(3) }, () => pick(r.cubes[0].dims)))];
    const where = Array.from({ length: rand(4) }, filter);
    const metric = pick(METRICS);
    const alias = metric === 'count' ? 'count' : metric.replace('.', '_');
    // The API leaves ties in a metric's order to SQLite, so the groups break them here.
    const order = rand(2) ? [`${alias}.${pick(['asc', 'desc'])}`, ...group.map((g) => `${g}.asc`)] : group.map((g) => `${g}.${pick(['asc', 'desc'])}`);
    const qs = [...where, `metric=${metric}`, ...(group.length ? [`group=${group.join(',')}`] : []), ...(order.length ? [`order=${order.join(',')}`] : []), `limit=${1 + rand(12)}`, `offset=${rand(3)}`].join('&');
    const got = aggregate(r, new URLSearchParams(qs));
    const need = new Set([...group, ...where.map((w) => w.split('=')[0])]);
    const field = metric.split('.')[1];
    const fits = r.cubes.some((c) => [...need].every((f) => c.dims.includes(f)) && (!field || field in c.metrics));
    if (!fits) { assert.equal(got, null, qs); continue; }
    taken++;
    assert.ok(got, `the rollup should take ${qs}`);
    const want = viaSQL(qs);
    assert.deepEqual(got.rows, want.rows, qs);
    assert.equal(got.more, want.more, qs);
    assert.equal(got.matched, count(where), qs);
  }
  assert.ok(taken > 1500, `only ${taken} of 2000 queries were taken`);
});

test('a query outside the rollup falls through', () => {
  const r = R();
  for (const qs of [
    'group=speed',
    'speed=eq.60',
    'metric=sum.year',
    'metric=avg.lga',
    'metric=count.lga',
    'year=eq.abc',
    'year=like.20*',
    'fatal=eq.maybe',
    'order=nope.desc',
    'limit=0',
    'lga=2020',
  ]) assert.equal(aggregate(r, new URLSearchParams(qs)), null, qs);
});

// R2 as the function sees it: each rollup carries the identity of the Parquet it was built from.
function r2(parquets, rollups) {
  const meta = (k) => ({ customMetadata: { parquet: rollups[k] }, etag: 'r' });
  return {
    get: async (k) => (k in rollups ? { ...meta(k), body: new Response(gz).body } : null),
    head: async (k) => (k in rollups ? meta(k) : k in parquets ? { customMetadata: { sha256: parquets[k] }, etag: 'e' } : null),
  };
}

test('rollup() reads R2, names the version and its provenance, and skips withheld datasets', async () => {
  const assets = { '/latest.json': { t: '2026-01-01' }, '/d/t/versions.json': { versions: [{ version: '2026-01-01' }, { version: '2025-01-01' }, { version: '2024-01-01' }] } };
  const pq = { 'd/t/v/2026-01-01/data.parquet': 'a1', 'd/t/v/2025-01-01/data.parquet': 'b1', 'd/t/v/2024-01-01/data.parquet': 'c1' };
  const ctx = {
    env: {
      DB: {},
      ASSETS: { fetch: async (q) => { const p = new URL(q.url).pathname; return p in assets ? Response.json(assets[p]) : new Response('', { status: 404 }); } },
      DIST: r2(pq, { '_rollup/t/2026-01-01.json.gz': 'sha256:a1' }),
    },
  };
  const a = await rollup(ctx, 't', undefined, 'aggregate', ['severity=eq.Fatal', 'metric=count', 'group=year', 'order=count.desc,year.asc', 'limit=3']);
  assert.equal(a.version, '2026-01-01');
  assert.equal(a.attribution, 'A');
  assert.equal(a.matched, count(['severity=eq.Fatal']));
  assert.match(a.query, /\/api\/v1\/datasets\/t\/versions\/2026-01-01\/aggregate\?severity=eq\.Fatal/);
  // A second-newest version is taken when R2 holds its rollup; an older one never is.
  assert.equal(await rollup(ctx, 't', '2025-01-01', 'aggregate', ['metric=count']), null);
  ctx.env.DIST = r2(pq, { '_rollup/t/2025-01-01.json.gz': 'sha256:b1', '_rollup/t/2024-01-01.json.gz': 'sha256:c1' });
  assert.equal((await rollup(ctx, 't', '2025-01-01', 'aggregate', ['metric=count'])).version, '2025-01-01');
  assert.equal(await rollup(ctx, 't', '2024-01-01', 'aggregate', ['metric=count']), null);
  assert.equal(await rollup(ctx, 'gone', undefined, 'aggregate', ['metric=count']), null);
  // A slug that names a property every object inherits is not a dataset.
  assert.equal(await rollup(ctx, 'constructor', undefined, 'aggregate', ['metric=count']), null);
  assert.equal(await rollup(ctx, 't', undefined, 'rows', ['limit=5']), null);
  // Without D1 the cited query URL gives 503, so the rollup does not answer either.
  assert.equal(await rollup({ env: { ...ctx.env, DB: undefined } }, 't', '2025-01-01', 'aggregate', ['metric=count']), null);
});

test('a rollup built from other bytes than the published Parquet never answers', async () => {
  // latest.json was read once by the test above; this version is the newest in versions.json.
  const assets = { '/d/t/versions.json': { versions: [{ version: '2026-03-01' }, { version: '2026-01-01' }] } };
  const env = {
    DB: {},
    ASSETS: { fetch: async (q) => { const p = new URL(q.url).pathname; return p in assets ? Response.json(assets[p]) : new Response('', { status: 404 }); } },
  };
  const k = '_rollup/t/2026-03-01.json.gz', pk = 'd/t/v/2026-03-01/data.parquet';
  // Rebuilt under --replace: R2 publishes new Parquet and the rollup still names the old.
  env.DIST = r2({ [pk]: 'new' }, { [k]: 'sha256:old' });
  assert.equal(await rollup({ env }, 't', '2026-03-01', 'aggregate', ['metric=count']), null);
  // A rollup written before rollups were stamped is not trusted either.
  env.DIST = r2({ [pk]: 'new' }, { [k]: undefined });
  assert.equal(await rollup({ env }, 't', '2026-03-01', 'aggregate', ['metric=count']), null);
  // An object pushed without a stored SHA-256 is named by its ETag.
  env.DIST = r2({}, { [k]: 'etag:e' });
  env.DIST.head = async (x) => (x === k ? { customMetadata: { parquet: 'etag:e' } } : x === pk ? { customMetadata: {}, etag: 'e' } : null);
  assert.ok(await rollup({ env }, 't', '2026-03-01', 'aggregate', ['metric=count']));
  // Once the Parquet changes, the answer stops as soon as the cached check lapses.
  const now = Date.now;
  try {
    env.DIST.head = async (x) => (x === k ? { customMetadata: { parquet: 'etag:e' } } : x === pk ? { customMetadata: { sha256: 'z' }, etag: 'f' } : null);
    assert.ok(await rollup({ env }, 't', '2026-03-01', 'aggregate', ['metric=count']), 'still within the check interval');
    Date.now = () => now() + 61_000;
    assert.equal(await rollup({ env }, 't', '2026-03-01', 'aggregate', ['metric=count']), null);
  } finally {
    Date.now = now;
  }
});

test('LIKE patterns match in linear time and as SQLite matches them', () => {
  const ref = (p, s) => new RegExp('^' + p.split('*').map((x) => x.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('[\\s\\S]*') + '$').test(s);
  const alpha = ['a', 'b', 'e', '*', '%', '_', '𝔄'];
  for (let i = 0; i < 5000; i++) {
    const p = Array.from({ length: rand(7) }, () => pick(alpha)).join('');
    const s = Array.from({ length: rand(9) }, () => pick(alpha.filter((c) => c !== '*'))).join('');
    assert.equal(glob(p)(s), ref(p, s), `${p} on ${s}`);
  }
  // Eight wildcards over a long value took two minutes as a backtracking regex.
  const long = 'e'.repeat(50_000);
  const t = performance.now();
  assert.equal(glob('*e*e*e*e*e*e*e*e*#')(long), false);
  assert.ok(performance.now() - t < 200);
});
