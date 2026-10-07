import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { test } from 'node:test';
import { BUDGET, BudgetError, openVersion, parquetRows } from './_parquet.js';
import { onRequestPost } from './mcp.js';

// A register shaped like ASIC's, with query: false, so D1 holds none of it: the rollup answers
// counts and the Parquet engine answers rows. Every answer is held to DuckDB's on the published
// file, written by pipeline/tests/test_company_fixture.py.
const fixture = (name) => readFileSync(new URL(`../pipeline/tests/fixtures/companies/${name}`, import.meta.url));
const data = fixture('data.parquet'), profiled = fixture('profiled.parquet'), gz = fixture('rollup.json.gz');
const want = JSON.parse(fixture('expected.json'));
const SLUG = want.slug, V = want.version;
const PUB = `d/${SLUG}/v/${V}/data.parquet`, Q = `_q/${SLUG}/${V}.parquet`, ROLLUP = `_rollup/${SLUG}/${V}.json.gz`;
const sha = createHash('sha256').update(data).digest('hex');

const objects = new Map([[PUB, data], [Q, profiled], [ROLLUP, gz]]);
const reads = [];
const DIST = {
  async get(key, opts = {}) {
    const b = objects.get(key);
    if (!b) return null;
    reads.push(key);
    if (key === ROLLUP) return { customMetadata: { parquet: `sha256:${sha}` }, body: new Response(b).body };
    const r = opts.range || {};
    const start = r.suffix !== undefined ? Math.max(0, b.length - r.suffix) : r.offset || 0;
    const end = r.suffix !== undefined ? b.length : r.length !== undefined ? start + r.length : b.length;
    const out = b.subarray(start, end);
    return { size: b.length, arrayBuffer: async () => out.buffer.slice(out.byteOffset, out.byteOffset + out.length) };
  },
  async head(key) {
    if (key === ROLLUP) return objects.has(key) ? { customMetadata: { parquet: `sha256:${sha}` } } : null;
    return key === PUB ? { customMetadata: { sha256: sha }, etag: 'e' } : null;
  },
};
// D1 is bound and holds no version of this dataset, as for an entry with query: false.
const sql = new DatabaseSync(':memory:');
sql.exec('CREATE TABLE _versions (slug TEXT, version TEXT, tbl TEXT, fields TEXT, rows INTEGER, attribution TEXT, header TEXT)');
const DB = {
  prepare(q) {
    let binds = [];
    const st = { bind(...b) { binds = b; return st; }, async first() { const r = sql.prepare(q).get(...binds); return r ? { ...r } : null; } };
    return st;
  },
};
globalThis.caches = { default: { match: async () => undefined, put: async () => {} } };
const assets = { '/latest.json': { [SLUG]: V } };
const env = {
  DB, DIST,
  ASSETS: { fetch: async (r) => { const p = new URL(r.url || r).pathname; return p in assets ? Response.json(assets[p]) : new Response('', { status: 404 }); } },
};
let id = 0;
async function call(name, args) {
  const request = new Request('https://publicdata.au/mcp', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: ++id, method: 'tools/call', params: { name, arguments: args } }) });
  const r = await (await onRequestPost({ request, env, waitUntil() {} })).json();
  return r.result.isError ? { error: r.result.content[0].text } : r.result.structuredContent;
}
const fileReads = () => reads.filter((k) => k !== ROLLUP);

test('counts by type and registration date agree with DuckDB, and come from the rollup without reading the file', async () => {
  for (const c of want.counts) {
    reads.length = 0;
    const got = await call('count_rows', { slug: SLUG, where: c.where, group_by: c.group_by });
    const label = JSON.stringify(c.where) + ' by ' + c.group_by;
    assert.equal(got.error, undefined, label);
    assert.deepEqual(got.groups, c.groups, label);
    assert.equal(got.matched, c.matched, label);
    assert.equal(got.version, V);
    // A version D1 does not hold is cited by its files.
    assert.equal(got.file, `https://publicdata.au/${PUB}`);
    assert.equal(got.manifest, `https://publicdata.au/d/${SLUG}/v/${V}/manifest.json`);
    if ('acn' in c.where) assert.ok(fileReads().includes(Q), `${label} is not in a cube, so the profile copy answers it`);
    else assert.deepEqual(fileReads(), [], `${label} reads only the rollup`);
  }
});

const rowsVia = (q) => call('query_rows', { slug: SLUG, where: q.where, select: q.select, limit: q.limit });

test('a page of rows by type and year agrees with DuckDB, and the next page carries on from it', async () => {
  const [q] = want.rows;
  const got = await rowsVia(q);
  assert.equal(got.error, undefined);
  assert.deepEqual(got.rows, q.rows);
  assert.equal(got.matched, q.matched);
  assert.equal(got.next_offset, q.limit);
  const next = await call('query_rows', { slug: SLUG, where: q.where, select: q.select, limit: 5, offset: q.limit - 5 });
  assert.deepEqual(next.rows, q.rows.slice(-5));
});

test('a page the budget refuses for counting every match takes its count from the rollup', async () => {
  const q = want.rows[2];
  const entry = await openVersion(env, SLUG, V);
  const params = new URLSearchParams(`status=eq.DRGD&limit=${q.limit}&select=${q.select.join(',')}`);
  const was = BUDGET.values;
  BUDGET.values = 3_000;
  try {
    await assert.rejects(parquetRows(env, entry, params, `https://publicdata.au/${PUB}`), BudgetError);
    const got = await rowsVia(q);
    assert.equal(got.error, undefined);
    assert.deepEqual(got.rows, q.rows);
    assert.equal(got.matched, q.matched);
    // An order needs every match, so the count does not help it and the budget still refuses.
    assert.match((await call('query_rows', { slug: SLUG, where: q.where, order: 'acn.desc' })).error, /DuckDB SQL/);
    // A filter no cube holds has no count to take, so the refusal stands.
    assert.match((await rowsVia({ ...q, where: { ...q.where, acn: { min: '000000001' } } })).error, /DuckDB SQL/);
  } finally {
    BUDGET.values = was;
  }
});

test('the known-count page is the page a full scan gives, at every offset', async () => {
  const entry = await openVersion(env, SLUG, V);
  const url = `https://publicdata.au/${PUB}`;
  for (const qs of [
    'type=eq.APTY&registration_date=gte.2024-01-01&registration_date=lte.2024-12-31',
    'registration_date=is.null',
    'current_name_indicator=is.null&status=eq.DRGD',
    'type=in.(FNOS,CCIV)&registration_date=lt.1980-01-01',
    'status=neq.REGD',
    'acn=gte.000020000',
  ]) {
    for (const [limit, offset] of [[1, 0], [7, 3], [100, 0], [25, 400], [5, 6000]]) {
      const params = new URLSearchParams(`${qs}&limit=${limit}&offset=${offset}&select=acn,company_name`);
      const full = await parquetRows(env, entry, params, url);
      const known = await parquetRows(env, entry, params, url, BUDGET, full.matched);
      assert.deepEqual(known.rows, full.rows, `${qs} ${limit} ${offset}`);
      assert.equal(known.more, full.more, `${qs} ${limit} ${offset}`);
      assert.equal(known.matched, full.matched);
    }
  }
});

test('an ACN lookup reads the profile copy and never the rollup', async () => {
  const q = want.rows[1];
  reads.length = 0;
  const got = await rowsVia(q);
  assert.deepEqual(got.rows, q.rows);
  assert.equal(got.matched, q.matched);
  assert.ok(!reads.includes(ROLLUP));
});

test('without its rollup the count falls through to the profile copy and agrees', async () => {
  objects.delete(ROLLUP);
  try {
    const [c] = want.counts;
    const got = await call('count_rows', { slug: SLUG, where: c.where, group_by: c.group_by });
    assert.deepEqual(got.groups, c.groups);
    assert.equal(got.matched, c.matched);
  } finally {
    objects.set(ROLLUP, gz);
  }
});
