import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { test } from 'node:test';
import { parquetReadObjects } from 'hyparquet';
import { decompress } from 'fzstd';
import { answer } from './_api.js';
import { aggregateQuery, rowsQuery } from './_query.js';
import { BUDGET, BudgetError, matches, openVersion, parquetAggregate, parquetRows, prepare } from './_parquet.js';
import { onRequestPost } from './mcp.js';
import { onRequestGet as dFile } from './d/[[path]].js';

// The fixtures are written by the pipeline's Parquet writer with eight rows to a group
// (pipeline/tests/test_query_fixture.py), so five groups, one per year, can be pruned. The two
// under the query profile carry its footer key and year as INT32; the sorted one is ordered by
// year and place with a page index of two-row pages, and the other has no page index.
const fixture = (name) => readFileSync(new URL(`../pipeline/tests/fixtures/parquet/${name}`, import.meta.url));
const bytes = fixture('rows-profiled.parquet');
const unsortedBytes = fixture('rows-profiled-unsorted.parquet');
const plain = fixture('rows.parquet');
const SLUG2 = 'crashes-unsorted';
const SLUG = 'crashes', NEWEST = '2026-04-24', OLDER = '2025-01-01', UNSORTED = '2024-01-01';
const URL_ = `https://publicdata.au/d/${SLUG}/v/${OLDER}/data.parquet`;

// R2 as the binding behaves: suffix and offset ranges, and the size of the whole object.
const objects = new Map();
const reads = [];
const DIST = {
  async get(key, opts = {}) {
    const b = objects.get(key);
    if (!b) return null;
    const r = opts.range || {};
    const start = r.suffix !== undefined ? Math.max(0, b.length - r.suffix) : r.offset || 0;
    const end = r.suffix !== undefined ? b.length : r.length !== undefined ? start + r.length : b.length;
    reads.push({ key, start, end });
    const out = b.subarray(start, end);
    return { size: b.length, arrayBuffer: async () => out.buffer.slice(out.byteOffset, out.byteOffset + out.length) };
  },
  async head() { return null; },
};
// The published file at d/, and the build's internal profile copy at _q/ that no route serves.
const put = (version, b = bytes, slug = SLUG) => objects.set(`d/${slug}/v/${version}/data.parquet`, b);
const putQ = (version, b = bytes, slug = SLUG) => objects.set(`_q/${slug}/${version}.parquet`, b);
put(NEWEST);
put(OLDER, plain);
putQ(OLDER);
put(UNSORTED, plain);
put(OLDER, unsortedBytes, SLUG2);

// D1 holds the newest version, over the same rows, so every Parquet answer for the older one can
// be held to the answer the query API gives.
const entry = await openVersion({ DIST }, SLUG, OLDER);
const unsorted = await openVersion({ DIST }, SLUG2, OLDER);
const fields = entry.fields;
const sql = new DatabaseSync(':memory:');
sql.exec('CREATE TABLE _versions (slug TEXT, version TEXT, tbl TEXT, fields TEXT, rows INTEGER, attribution TEXT, header TEXT)');
async function load(slug, tbl, b, e) {
  const all = await parquetReadObjects({ file: b.buffer.slice(b.byteOffset, b.byteOffset + b.length), compressors: { ZSTD: (i, n) => decompress(i, new Uint8Array(n)) } });
  sql.exec(`CREATE TABLE ${tbl} (lga TEXT, year INTEGER, fatal INTEGER, speed REAL, day TEXT, seen TEXT, ref REAL)`);
  sql.prepare('INSERT INTO _versions VALUES (?, ?, ?, ?, ?, ?, ?)').run(slug, NEWEST, tbl, JSON.stringify(e.fields), all.length, e.header.attribution, JSON.stringify(e.header));
  const ins = sql.prepare(`INSERT INTO ${tbl} VALUES (?, ?, ?, ?, ?, ?, ?)`);
  for (const r of all) {
    ins.run(r.lga, Number(r.year), r.fatal === null ? null : r.fatal ? 1 : 0, r.speed, r.day && r.day.toISOString().slice(0, 10), r.seen && r.seen.toISOString().slice(0, 19), Number(r.ref));
  }
}
await load(SLUG, 't', bytes, entry);
await load(SLUG2, 't2', unsortedBytes, unsorted);
const DB = {
  prepare(q) {
    let binds = [];
    const st = {
      bind(...b) { binds = b; return st; },
      async first() { const r = sql.prepare(q).get(...binds); return r ? { ...r } : null; },
      async all() { return { results: sql.prepare(q).all(...binds).map((r) => ({ ...r })) }; },
    };
    return st;
  },
};
globalThis.caches = { default: { match: async () => undefined, put: async () => {} } };
const assets = { '/latest.json': { [SLUG]: NEWEST, [SLUG2]: NEWEST } };
const env = {
  DB, DIST,
  ASSETS: { fetch: async (r) => { const p = new URL(r.url || r).pathname; return p in assets ? Response.json(assets[p]) : new Response('', { status: 404 }); } },
};

async function d1(op, qs, slug = SLUG) {
  const request = new Request(`https://publicdata.au/api/v1/datasets/${slug}/${op}?${qs}`);
  const r = await answer({ request, env, params: { slug }, waitUntil() {} }, op === 'rows' ? rowsQuery : aggregateQuery, op);
  const b = await r.json();
  assert.equal(r.status, 200, `${qs}: ${b.error} ${b.detail}`);
  return b;
}
// The one value D1 cannot hold exactly is left out of the comparison and checked on its own.
const noRef = (rows) => rows.map(({ ref, ...r }) => r);
// Both profile files, sorted with a page index and in the publisher's order without one.
const FILES = [[SLUG, entry], [SLUG2, unsorted]];

test('rows match the query API answer for every filter, order and page', async () => {
  const cases = [
    '', 'year=eq.2019', 'year=gte.2020&lga=eq.Logan', 'lga=is.null', 'lga=not.is.null&year=lt.2020',
    'lga=in.(Brisbane,Cairns)', 'lga=ilike.*coast*', 'lga=like.Gold*', 'lga=like.gold*', 'lga=like.*_*', 'lga=ilike.LO*N', 'fatal=eq.true', 'fatal=is.null',
    'speed=gt.55&order=speed.desc,lga.asc', 'order=lga.asc,year.desc&limit=7&offset=3', 'day=gte.2020-06-01',
    'seen=lt.2026-04-24T10:00:00', 'lga=neq.Logan', 'year=not.eq.2018&limit=5&offset=30', 'select=year,lga&limit=3&offset=38',
    'year=eq.2030', 'order=fatal.asc,day.desc&limit=12', 'day=is.null', 'day=gte.2019-06-01&day=lt.2021-01-01', 'seen=not.is.null&order=seen.desc&limit=4',
  ];
  for (const [slug, e] of FILES) for (const qs of cases) {
    const want = await d1('rows', qs, slug);
    const got = await parquetRows(env, e, new URLSearchParams(qs), URL_);
    assert.deepEqual(noRef(got.rows), noRef(want.rows), `${slug} ${qs}`);
    assert.equal(got.more, want.next !== null, `${slug} ${qs}`);
    const count = await d1('aggregate', qs.split('&').filter((p) => !/^(order|limit|offset|select)=/.test(p)).join('&'), slug);
    assert.equal(got.matched, count.rows[0].count, `${slug} ${qs}`);
  }
});

test('aggregates match the query API answer', async () => {
  const cases = [
    'group=lga&metric=count&order=count.desc,lga.asc', 'group=year&metric=sum.speed,avg.speed,min.day,max.lga,count.fatal',
    'metric=count&year=eq.2030', 'metric=sum.fatal,min.speed,max.seen', 'group=fatal,year&metric=count&limit=4&offset=2',
    'group=lga&metric=sum.fatal&lga=not.is.null&order=sum_fatal.desc,lga.asc&limit=2', 'group=year&metric=count&year=gte.2021',
  ];
  for (const [slug, e] of FILES) for (const qs of cases) {
    const want = await d1('aggregate', qs, slug);
    const got = await parquetAggregate(env, e, new URLSearchParams(qs), URL_);
    assert.deepEqual(got.rows, want.rows, `${slug} ${qs}`);
    assert.equal(got.more, want.next !== null, `${slug} ${qs}`);
  }
});

test('row groups and pages are pruned on their statistics, and a proven match reads no filter column', async () => {
  const lga = (i) => entry.groups[i].chunks.lga;
  const one = await parquetRows(env, entry, new URLSearchParams('year=eq.2019&select=lga'), URL_);
  assert.equal(one.matched, 8);
  // Every row of 2019's group matches, so only the selected column of that group is read.
  assert.deepEqual([one.used.groups, one.used.values], [1, 8]);
  assert.ok(one.used.bytes <= lga(1).end - lga(1).start);
  const place = await parquetAggregate(env, entry, new URLSearchParams('year=eq.2019&lga=eq.Logan'), URL_);
  assert.equal(place.rows[0].count, 1);
  // Sorted by place within the year, so only the page holding Logan is decoded, in both columns.
  assert.deepEqual([place.used.groups, place.used.values], [1, 4]);
  const count = await parquetAggregate(env, entry, new URLSearchParams('year=gte.2021'), URL_);
  assert.equal(count.rows[0].count, 16);
  assert.equal(count.used.groups, 0);
  assert.equal((await parquetAggregate(env, entry, new URLSearchParams(''), URL_)).used.groups, 0);
  assert.equal((await parquetAggregate(env, entry, new URLSearchParams('year=eq.1999&lga=eq.Logan'), URL_)).used.groups, 0);
});

test('the footer and page index are read once per version', async () => {
  await parquetAggregate(env, entry, new URLSearchParams('group=lga&year=eq.2020'), URL_);
  reads.length = 0;
  assert.equal(entry, await openVersion({ DIST }, SLUG, OLDER));
  await parquetAggregate(env, entry, new URLSearchParams('group=lga&year=eq.2020&limit=5'), URL_);
  // Only data pages are read the second time.
  assert.ok(reads.length > 0);
  const pagesAt = new Set(entry.groups.flatMap((g) => Object.values(g.chunks).flatMap((c) => [c.ci && c.ci.start, c.oi && c.oi.start])));
  assert.ok(reads.every((r) => !pagesAt.has(r.start)), JSON.stringify(reads));
});

test('a file written before the query profile is refused with DuckDB SQL, before any data is read', async () => {
  const old = await openVersion({ DIST }, SLUG, UNSORTED);
  assert.equal(old.profiled, false);
  assert.equal(entry.profiled, true);
  assert.equal(unsorted.profiled, true);
  reads.length = 0;
  const url = `https://publicdata.au/d/${SLUG}/v/${UNSORTED}/data.parquet`;
  await assert.rejects(parquetAggregate(env, old, new URLSearchParams('group=lga&year=eq.2019'), url), (e) => {
    assert.ok(e instanceof BudgetError);
    assert.match(e.message, /written before the query profile/);
    assert.ok(e.message.endsWith(`SELECT "lga", COUNT(*) AS "count" FROM '${url}' WHERE "year" = 2019 GROUP BY "lga" ORDER BY "lga" LIMIT 100 OFFSET 0`), e.message);
    return true;
  });
  assert.equal(reads.length, 0);
  await assert.rejects(parquetRows(env, old, new URLSearchParams('nope=eq.1'), url), /no field nope/);
});

test('a query over the budget is refused with DuckDB SQL that answers it from the file', async () => {
  const small = { ...BUDGET, groups: 2 };
  await assert.rejects(parquetRows(env, entry, new URLSearchParams('lga=not.is.null&fatal=eq.true&order=day.desc'), URL_, small), (e) => {
    assert.ok(e instanceof BudgetError);
    assert.match(e.message, /would read 5 row groups .*\(2 row groups/);
    assert.match(e.message, /Narrow where/);
    assert.ok(e.message.endsWith(`SELECT "lga", "year", "fatal", "speed", "day", "seen", "ref" FROM '${URL_}' WHERE NOT ("lga" IS NULL) AND "fatal" = true ORDER BY "day" DESC LIMIT 100 OFFSET 0`), e.message);
    return true;
  });
  await assert.rejects(parquetAggregate(env, entry, new URLSearchParams("group=lga&metric=avg.speed&lga=ilike.*o'c*"), URL_, { ...BUDGET, bytes: 100 }), (e) => {
    assert.match(e.message, /MB/);
    assert.ok(e.message.endsWith(`SELECT "lga", AVG("speed") AS "avg_speed" FROM '${URL_}' WHERE "lga" ILIKE '%o''c%' ESCAPE '\\' GROUP BY "lga" ORDER BY "lga" LIMIT 100 OFFSET 0`), e.message);
    return true;
  });
  // Within budget, the same queries answer.
  assert.ok((await parquetRows(env, entry, new URLSearchParams('lga=not.is.null&fatal=eq.true&order=day.desc'), URL_)).rows.length);
});

test('64-bit integers come back as numbers while exact and as text beyond, and types read as D1 gives them', async () => {
  const r = await parquetRows(env, entry, new URLSearchParams('ref=eq.1000&select=ref,year,fatal,day,seen'), URL_);
  assert.deepEqual(r.rows, [{ ref: 1000, year: 2018, fatal: 0, day: '2018-02-02', seen: '2026-04-24T01:01:05' }]);
  // year is INT32 under the profile and decodes to plain numbers, as ref's INT64 does while exact.
  assert.equal(entry.elements.get('year').type, 'INT32');
  assert.equal(entry.elements.get('ref').type, 'INT64');
  assert.deepEqual(fields.map((f) => [f.name, f.type]), [['lga', 'string'], ['year', 'integer'], ['fatal', 'boolean'], ['speed', 'number'], ['day', 'date'], ['seen', 'datetime'], ['ref', 'integer']]);
  const big = await parquetRows(env, entry, new URLSearchParams('ref=gt.9007199254740000&select=ref'), URL_);
  assert.deepEqual(big.rows.map((x) => x.ref).sort(), ['9007199254741001', '9007199254741011', '9007199254741021', '9007199254741031']);
});

let id = 0;
async function call(name, args) {
  const request = new Request('https://publicdata.au/mcp', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: ++id, method: 'tools/call', params: { name, arguments: args } }) });
  const r = await (await onRequestPost({ request, env, waitUntil() {} })).json();
  return r.result.isError ? { error: r.result.content[0].text } : r.result.structuredContent;
}

test('D1 answers the versions it holds and the file answers the rest, naming the version', async () => {
  const newest = await call('query_rows', { slug: SLUG, where: { year: 2019 }, select: ['lga'], limit: 3 });
  assert.equal(newest.version, NEWEST);
  assert.equal(newest.file, undefined);
  assert.equal(newest.query, `https://publicdata.au/api/v1/datasets/${SLUG}/rows?year=eq.2019&limit=3&offset=0&select=lga`);
  const older = await call('query_rows', { slug: SLUG, version: OLDER, where: { year: 2019 }, select: ['lga'], limit: 3 });
  assert.equal(older.version, OLDER);
  assert.equal(older.matched, 8);
  assert.equal(older.next_offset, 3);
  assert.deepEqual(older.rows, newest.rows);
  assert.equal(older.attribution, entry.header.attribution);
  assert.equal(older.file, URL_);
  assert.equal(older.query, `https://publicdata.au/api/v1/datasets/${SLUG}/versions/${OLDER}/rows?year=eq.2019&limit=3&offset=0&select=lga`);
  const groups = await call('count_rows', { slug: SLUG, version: OLDER, group_by: ['year'], where: { lga: 'Logan' } });
  assert.deepEqual(groups.groups, [[2018, 2], [2020, 2], [2022, 2], [2019, 1], [2021, 1]].map(([year, count]) => ({ year, count })));
  assert.match((await call('count_rows', { slug: SLUG, version: UNSORTED, group_by: ['lga'] })).error, /DuckDB SQL.*GROUP BY "lga"/);
  assert.match((await call('query_rows', { slug: SLUG, version: '2020-01-01' })).error, /has no version 2020-01-01/);
  assert.match((await call('count_rows', { slug: SLUG, version: OLDER, where: { nope: 1 } })).error, /no field nope/);
});

test('the budget error reaches the agent, and a file that cannot be read says where the files are', async () => {
  const was = BUDGET.groups;
  BUDGET.groups = 1;
  try {
    assert.match((await call('count_rows', { slug: SLUG, version: OLDER, group_by: ['lga'] })).error, /Narrow where.*DuckDB SQL.*GROUP BY "lga"/);
  } finally {
    BUDGET.groups = was;
  }
  putQ(OLDER, bytes.subarray(0, 100));
  try {
    const e = (await call('count_rows', { slug: SLUG, version: OLDER, group_by: ['speed'] })).error;
    assert.match(e, /could not be read; its files are at/);
    assert.doesNotMatch(e, /_q/);
  } finally {
    putQ(OLDER);
  }
});

test('a profile file without a page index is read a column chunk at a time', async () => {
  assert.ok(unsorted.groups.every((g) => Object.values(g.chunks).every((c) => !c.oi)));
  const url = `https://publicdata.au/d/${SLUG2}/v/${OLDER}/data.parquet`;
  // The year statistics of each row group still rule out the others.
  const one = await parquetAggregate(env, unsorted, new URLSearchParams('year=eq.2019&group=lga'), url);
  assert.equal(one.used.groups, 1);
  assert.equal(one.matched, 8);
  const r = await call('query_rows', { slug: SLUG2, version: OLDER, where: { year: { min: 2021 } }, select: ['year'], limit: 2 });
  assert.deepEqual(r.rows, [{ year: 2021 }, { year: 2021 }]);
  assert.equal(r.matched, 16);
});

test('like and ilike ignore case in ASCII letters only, as the query API does', () => {
  const like = (op, pattern, v) => matches(prepare([{ name: 'lga', op, not: false, args: [pattern] }])[0], v);
  for (const op of ['like', 'ilike']) {
    assert.equal(like(op, 'gold*', 'Gold Coast'), true, op);
    assert.equal(like(op, 'GOLD COAST', 'Gold Coast'), true, op);
    assert.equal(like(op, 'ÉDEN*', 'Éden Park'), true, op);
    assert.equal(like(op, 'éden*', 'Éden Park'), false, op);
    assert.equal(like(op, '100%_*', '100%_rural'), true, op);
    assert.equal(like(op, '100%_*', '100xyrural'), false, op);
  }
});

test('the internal profile copy answers first, and answers still name the published file', async () => {
  assert.equal(entry.key, `_q/${SLUG}/${OLDER}.parquet`);
  assert.equal(unsorted.key, `d/${SLUG2}/v/${OLDER}/data.parquet`);
  assert.equal((await openVersion({ DIST }, SLUG, UNSORTED)).key, `d/${SLUG}/v/${UNSORTED}/data.parquet`);
  reads.length = 0;
  const r = await call('count_rows', { slug: SLUG, version: OLDER, group_by: ['lga'], where: { year: 2022 } });
  assert.ok(reads.length && reads.every((x) => x.key.startsWith('_q/')));
  assert.equal(r.file, URL_);
  assert.doesNotMatch(JSON.stringify(r), /_q/);
  const was = BUDGET.groups;
  BUDGET.groups = 1;
  try {
    const e = (await call('query_rows', { slug: SLUG, version: OLDER, where: { lga: 'Logan' } })).error;
    assert.match(e, new RegExp(`FROM '${URL_}'`));
    assert.doesNotMatch(e, /_q/);
  } finally {
    BUDGET.groups = was;
  }
});

test('no /d/ request reaches the _q/ copies', async () => {
  const asked = [];
  const r2 = { get: async (k) => { asked.push(k); return null; }, head: async (k) => { asked.push(k); return null; }, list: async () => ({ objects: [] }) };
  const denv = { DIST: r2, ASSETS: { fetch: async () => new Response('', { status: 404 }) } };
  const tries = [
    `/d/${SLUG}/v/${OLDER}/../../../../_q/${SLUG}/${OLDER}.parquet`,
    `/d/%2e%2e/_q/${SLUG}/${OLDER}.parquet`,
    `/d/${SLUG}/v/${OLDER}/..%2F..%2F..%2F..%2F_q%2F${SLUG}%2F${OLDER}.parquet`,
    `/d/%2F_q/${SLUG}/${OLDER}.parquet`,
    `/d/${SLUG}/v/${OLDER}/%5C..%5C..%5C_q%5C${SLUG}.parquet`,
  ];
  for (const t of tries) {
    const url = new URL('https://publicdata.au' + t);
    // Pages runs the /d/ function only on /d/ paths, after the URL is normalised.
    if (!url.pathname.startsWith('/d/')) continue;
    const res = await dFile({ request: new Request(url), env: denv });
    assert.notEqual(res.status, 200, t);
  }
  assert.ok(asked.length > 0);
  assert.ok(asked.every((k) => k.startsWith('d/')), JSON.stringify(asked));
  for (const t of tries.slice(0, 2)) assert.ok(!new URL('https://publicdata.au' + t).pathname.startsWith('/d/'), t);
});
