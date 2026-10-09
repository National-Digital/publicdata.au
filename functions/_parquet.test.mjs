import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { DatabaseSync } from 'node:sqlite';
import { test } from 'node:test';
import { parquetReadObjects } from 'hyparquet';
import { decompress } from 'fzstd';
import { answer } from './_api.js';
import { aggregateQuery, rowsQuery } from './_query.js';
import { BUDGET, BudgetError, addInto, glob, matches, openVersion, parquetAggregate, parquetRows, prepare } from './_parquet.js';
import * as engine from './_parquet.js';
import { onRequestPost } from './mcp.js';
import { onRequestGet as dFile } from './d/[[path]].js';

// The one file a version is read from.
const open = async (env, slug, version) => {
  const at = await openVersion(env, slug, version);
  return at && at.files[0];
};

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

// R2 as the binding behaves: suffix and offset ranges, the size of the whole object, its ETag
// (an MD5, as R2 gives for an object put whole) and onlyIf, which a changed object fails with
// its metadata and no body.
const objects = new Map();
const reads = [];
const etagOf = (b) => createHash('md5').update(b).digest('hex');
const DIST = {
  async get(key, opts = {}) {
    const b = objects.get(key);
    if (!b) return null;
    const etag = etagOf(b);
    const want = opts.onlyIf && opts.onlyIf.etagMatches;
    if (want !== undefined && want !== etag) return { size: b.length, etag };
    const r = opts.range || {};
    const start = r.suffix !== undefined ? Math.max(0, b.length - r.suffix) : r.offset || 0;
    const end = r.suffix !== undefined ? b.length : r.length !== undefined ? start + r.length : b.length;
    reads.push({ key, start, end });
    const out = b.subarray(start, end);
    return { size: b.length, etag, arrayBuffer: async () => out.buffer.slice(out.byteOffset, out.byteOffset + out.length), text: async () => Buffer.from(out).toString('utf8') };
  },
  async head(key) {
    const b = objects.get(key);
    return b ? { size: b.length, etag: etagOf(b) } : null;
  },
};
// The published file at d/, and the build's internal profile copy at _q/ that no route serves.
const put = (version, b = bytes, slug = SLUG) => objects.set(`d/${slug}/v/${version}/data.parquet`, b);
const putQ = (version, b = bytes, slug = SLUG) => objects.set(`_q/${slug}/${version}.parquet`, b);
put(NEWEST);
put(OLDER, plain);
putQ(OLDER);
put(UNSORTED, plain);
put(OLDER, unsortedBytes, SLUG2);

// D1 holds the newest version of each slug, with the field list and values the loader gives it,
// so every Parquet answer for an older version can be held to the answer the query API gives.
const entry = await open({ DIST }, SLUG, OLDER);
const unsorted = await open({ DIST }, SLUG2, OLDER);
const D1_FIELDS = [
  { name: 'lga', type: 'string' }, { name: 'year', type: 'integer' }, { name: 'fatal', type: 'boolean' },
  { name: 'speed', type: 'number' }, { name: 'day', type: 'date' }, { name: 'seen', type: 'datetime' },
  { name: 'ref', type: 'integer' }, { name: 'suppressed', type: 'array' },
];
const fields = D1_FIELDS;
const sql = new DatabaseSync(':memory:');
sql.exec('CREATE TABLE _versions (slug TEXT, version TEXT, tbl TEXT, fields TEXT, rows INTEGER, attribution TEXT, header TEXT)');
const rowsOf = (b) => parquetReadObjects({ file: b.buffer.slice(b.byteOffset, b.byteOffset + b.length), compressors: { ZSTD: (i, n) => decompress(i, new Uint8Array(n)) } });
async function load(slug, tbl, b, header) {
  const all = await rowsOf(b);
  sql.exec(`CREATE TABLE ${tbl} (lga TEXT, year INTEGER, fatal INTEGER, speed REAL, day TEXT, seen TEXT, ref REAL, suppressed TEXT)`);
  sql.prepare('INSERT INTO _versions VALUES (?, ?, ?, ?, ?, ?, ?)').run(slug, NEWEST, tbl, JSON.stringify(D1_FIELDS), all.length, header.attribution, JSON.stringify(header));
  const ins = sql.prepare(`INSERT INTO ${tbl} VALUES (?, ?, ?, ?, ?, ?, ?, ?)`);
  for (const r of all) {
    ins.run(r.lga, Number(r.year), r.fatal === null ? null : r.fatal ? 1 : 0, r.speed, r.day && r.day.toISOString().slice(0, 10), r.seen && r.seen.toISOString().slice(0, 19), Number(r.ref), r.suppressed && r.suppressed.length ? r.suppressed.join(';') : null);
  }
}
// The published file keeps the publisher's order, and so does D1. The sorted copy is held to a
// table loaded in its own order, so ties and unordered pages can be compared exactly.
const SLUG3 = 'crashes-sorted';
await load(SLUG, 't', plain, entry.header);
await load(SLUG2, 't2', unsortedBytes, unsorted.header);
await load(SLUG3, 't3', bytes, entry.header);
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
const assets = { '/latest.json': { [SLUG]: NEWEST, [SLUG2]: NEWEST, [SLUG3]: NEWEST } };
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
// Both profile files: unsorted, in the publisher's order, and sorted with a page index.
const FILES = [[SLUG2, unsorted], [SLUG3, entry]];

test('rows match the query API answer for every filter, order and page', async () => {
  const cases = [
    '', 'year=eq.2019', 'year=gte.2020&lga=eq.Logan', 'lga=is.null', 'lga=not.is.null&year=lt.2020',
    'lga=in.(Brisbane,Cairns)', 'lga=ilike.*coast*', 'lga=like.Gold*', 'lga=like.gold*', 'lga=like.*_*', 'lga=ilike.LO*N', 'fatal=eq.true', 'fatal=is.null',
    'speed=gt.55&order=speed.desc,lga.asc', 'order=lga.asc,year.desc&limit=7&offset=3', 'day=gte.2020-06-01',
    'seen=lt.2026-04-24T10:00:00', 'lga=neq.Logan', 'year=not.eq.2018&limit=5&offset=30', 'select=year,lga&limit=3&offset=38',
    'year=eq.2030', 'order=fatal.asc,day.desc&limit=12', 'day=is.null', 'day=gte.2019-06-01&day=lt.2021-01-01', 'seen=not.is.null&order=seen.desc&limit=4',
    'select=suppressed,lga', 'suppressed=is.null', 'suppressed=like.*speed*', 'suppressed=eq.fatal;speed&select=lga,suppressed',
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
    'group=lga&metric=sum.fatal&lga=not.is.null&order=sum_fatal.desc,lga.asc&limit=2', 'group=year&metric=count&year=gte.2021', 'group=suppressed&metric=count,sum.speed&order=count.desc,suppressed.asc',
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
  assert.equal(entry, await open({ DIST }, SLUG, OLDER));
  await parquetAggregate(env, entry, new URLSearchParams('group=lga&year=eq.2020&limit=5'), URL_);
  // Only data pages are read the second time.
  assert.ok(reads.length > 0);
  const pagesAt = new Set(entry.groups.flatMap((g) => Object.values(g.chunks).flatMap((c) => [c.ci && c.ci.start, c.oi && c.oi.start])));
  assert.ok(reads.every((r) => !pagesAt.has(r.start)), JSON.stringify(reads));
});

test('a file written before the query profile is refused with DuckDB SQL, before any data is read', async () => {
  const old = await open({ DIST }, SLUG, UNSORTED);
  assert.equal(old.profiled, false);
  assert.equal(entry.profiled, true);
  assert.equal(unsorted.profiled, true);
  reads.length = 0;
  const url = `https://publicdata.au/d/${SLUG}/v/${UNSORTED}/data.parquet`;
  await assert.rejects(parquetAggregate(env, old, new URLSearchParams('group=lga&year=eq.2019'), url), (e) => {
    assert.ok(e instanceof BudgetError);
    assert.match(e.message, /written before the query profile/);
    assert.ok(e.message.endsWith(`SELECT "lga" AS "lga", COUNT(*) AS "count" FROM read_parquet('${url}', file_row_number = true) WHERE "year" = 2019 GROUP BY "lga" ORDER BY "lga" ASC NULLS FIRST LIMIT 100 OFFSET 0`), e.message);
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
    // The published file is in the publisher's order, so the SQL rebuilds the copy's order.
    assert.ok(e.message.endsWith(`SELECT "lga" AS "lga", "year" AS "year", "fatal" AS "fatal", "speed" AS "speed", "day" AS "day", "seen" AS "seen", "ref" AS "ref", NULLIF(array_to_string("suppressed", ';'), '') AS "suppressed" FROM read_parquet('${URL_}', file_row_number = true) WHERE NOT ("lga" IS NULL) AND "fatal" = true ORDER BY "day" DESC NULLS LAST, "year" ASC NULLS LAST, "lga" ASC NULLS LAST, file_row_number LIMIT 100 OFFSET 0`), e.message);
    return true;
  });
  await assert.rejects(parquetAggregate(env, entry, new URLSearchParams("group=lga&metric=avg.speed&lga=ilike.*o'c*"), URL_, { ...BUDGET, bytes: 100 }), (e) => {
    assert.match(e.message, /MB/);
    assert.ok(e.message.endsWith(`SELECT "lga" AS "lga", AVG("speed") AS "avg_speed" FROM read_parquet('${URL_}', file_row_number = true) WHERE "lga" ILIKE '%o''c%' ESCAPE '\\' GROUP BY "lga" ORDER BY "lga" ASC NULLS FIRST LIMIT 100 OFFSET 0`), e.message);
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
  assert.deepEqual(entry.fields, D1_FIELDS);
  assert.deepEqual(unsorted.fields, D1_FIELDS);
  assert.deepEqual(entry.fields.map((f) => [f.name, f.type]), [['lga', 'string'], ['year', 'integer'], ['fatal', 'boolean'], ['speed', 'number'], ['day', 'date'], ['seen', 'datetime'], ['ref', 'integer'], ['suppressed', 'array']]);
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
  // Without an order, D1 gives the publisher's order and the sorted copy its sort: the same
  // rows, in the order the order parameter describes.
  const all = async (version) => (await call('query_rows', { slug: SLUG, version, where: { year: 2019 }, select: ['lga'] })).rows.map((r) => r.lga);
  const [d1rows, fileRows] = [await all(undefined), await all(OLDER)];
  const byPlace = (a, b) => (a === null) - (b === null) || (a < b ? -1 : a > b ? 1 : 0);
  assert.deepEqual([...d1rows].sort(byPlace), fileRows);
  assert.notDeepEqual(d1rows, fileRows);
  assert.equal(older.manifest, `https://publicdata.au/d/${SLUG}/v/${OLDER}/manifest.json`);
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
  const get = DIST.get;
  DIST.get = async (key, o) => {
    if (o && o.range && o.range.offset !== undefined) throw new Error('R2 503');
    return get.call(DIST, key, o);
  };
  try {
    const e = (await call('count_rows', { slug: SLUG, version: OLDER, group_by: ['speed'] })).error;
    assert.match(e, /could not be read; its files are at/);
    assert.doesNotMatch(e, /_q/);
  } finally {
    DIST.get = get;
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
  assert.equal((await open({ DIST }, SLUG, UNSORTED)).key, `d/${SLUG}/v/${UNSORTED}/data.parquet`);
  reads.length = 0;
  const r = await call('count_rows', { slug: SLUG, version: OLDER, group_by: ['lga'], where: { year: 2022 } });
  assert.ok(reads.length && reads.every((x) => x.key.startsWith('_q/')));
  assert.equal(r.file, URL_);
  assert.doesNotMatch(JSON.stringify(r), /_q/);
  const was = BUDGET.groups;
  BUDGET.groups = 1;
  try {
    const e = (await call('query_rows', { slug: SLUG, version: OLDER, where: { lga: 'Logan' } })).error;
    assert.match(e, new RegExp(`read_parquet\\('${URL_}'`));
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
    `/d/${SLUG}/v/${OLDER}/_q/${SLUG}/${OLDER}.parquet`,
  ];
  for (const t of tries) {
    let url = new URL('https://publicdata.au' + t);
    // Pages runs the /d/ function only on /d/ paths, after the URL is normalised. An escaped path
    // is sent to its plain form, which is followed the same way.
    for (let hop = 0; hop < 3 && url.pathname.startsWith('/d/'); hop++) {
      const res = await dFile({ request: new Request(url), env: denv });
      assert.notEqual(res.status, 200, t);
      if (res.status !== 308) break;
      url = new URL(res.headers.get('location'), url);
    }
  }
  assert.ok(asked.length > 0);
  assert.ok(asked.every((k) => k.startsWith('d/')), JSON.stringify(asked));
  for (const t of tries.slice(0, 2)) assert.ok(!new URL('https://publicdata.au' + t).pathname.startsWith('/d/'), t);
});

test('a version is refused when its file carries no provenance', async () => {
  objects.set(`_q/${SLUG}/2023-01-01.parquet`, fixture('rows-no-provenance.parquet'));
  objects.set(`d/${SLUG}/v/2023-01-01/data.parquet`, fixture('rows-no-provenance.parquet'));
  assert.match((await call('count_rows', { slug: SLUG, version: '2023-01-01' })).error, /carries no provenance/);
});

test('a match the statistics prove is counted and paged without a row index, under a small heap', () => {
  // Twenty million rows; one index per row would need well over the 48 MB this child is given.
  const code = `
    import { readFileSync } from 'node:fs';
    import { openVersion, parquetRows, parquetAggregate } from './_parquet.js';
    const b = readFileSync(new URL('../pipeline/tests/fixtures/parquet/large-profiled.parquet', import.meta.url));
    const DIST = { async get(key, o = {}) {
      const r = o.range || {}, s = r.suffix !== undefined ? b.length - r.suffix : r.offset, e = r.suffix !== undefined ? b.length : s + r.length;
      const u = b.subarray(s, e); return { size: b.length, arrayBuffer: async () => u.buffer.slice(u.byteOffset, u.byteOffset + u.length) };
    } };
    const env = { DIST };
    const e = (await openVersion(env, 'big', '2026-01-01')).files[0];
    const out = [];
    for (const qs of ['limit=1', 'n=eq.0&limit=2&offset=19999990', 'n=gte.0&limit=1&offset=19999999']) {
      const r = await parquetRows(env, e, new URLSearchParams(qs), 'u');
      out.push([r.matched, r.rows, r.more, r.used.values]);
    }
    for (const qs of ['', 'n=eq.0', 'n=neq.1']) out.push((await parquetAggregate(env, e, new URLSearchParams(qs), 'u')).rows[0].count);
    console.log(JSON.stringify(out));
  `;
  const r = spawnSync(process.execPath, ['--max-old-space-size=48', '--input-type=module', '-e', code], { cwd: new URL('.', import.meta.url), encoding: 'utf8' });
  assert.equal(r.status, 0, r.stderr.slice(-500));
  const out = JSON.parse(r.stdout.trim().split('\n').pop());
  assert.deepEqual(out.slice(0, 3).map((x) => x.slice(0, 3)), [
    [20_000_000, [{ n: 0 }], true],
    [20_000_000, [{ n: 0 }, { n: 0 }], true],
    [20_000_000, [{ n: 0 }], false],
  ]);
  // Only the row group a page reads from is charged: this file has no page index, so that is one
  // group of 500,000 rows and never the twenty million.
  assert.ok(out.slice(0, 3).every((x) => x[3] === 500_000), JSON.stringify(out));
  assert.deepEqual(out.slice(3), [20_000_000, 20_000_000, 20_000_000]);
});

test('sums are compensated as SQLite sums, and like never backtracks', () => {
  const tenth = sql.prepare("WITH t(x) AS (VALUES (0.1),(0.1),(0.1),(0.1),(0.1),(0.1),(0.1),(0.1),(0.1),(0.1)) SELECT SUM(x) AS s, AVG(x) AS a FROM t").get();
  const acc = { n: 0, sum: 0, c: 0 };
  for (let k = 0; k < 10; k++) addInto(acc, 0.1);
  assert.equal(acc.sum + acc.c, tenth.s);
  assert.equal((acc.sum + acc.c) / 10, tenth.a);
  const start = Date.now();
  assert.equal(glob('*a'.repeat(12) + '*b', 'a'.repeat(30)), false);
  assert.ok(Date.now() - start < 100);
  assert.equal(glob('*a*b*', 'xxaxxbxx'), true);
  assert.equal(glob('a*', ''), false);
  assert.equal(glob('**', ''), true);
});

test('an ordered page counts offset + limit against the rows it may hold, before any data is read', async () => {
  const qs = (offset) => new URLSearchParams(`order=speed.desc,lga.asc&select=lga,speed&limit=2&offset=${offset}`);
  const small = { ...BUDGET, held: 6 };
  const ok = await parquetRows(env, entry, qs(3), URL_, small);
  assert.equal(ok.used.held, 6);
  assert.deepEqual(noRef(ok.rows), noRef((await d1('rows', qs(3).toString(), SLUG3)).rows));
  await parquetRows(env, entry, qs(0), URL_);
  reads.length = 0;
  await assert.rejects(parquetRows(env, entry, qs(4), URL_, small), (e) => {
    assert.ok(e instanceof BudgetError);
    assert.match(e.message, /7 ordered rows .*\(6 ordered rows\)/);
    // The advice keeps the tied rows: an inclusive bound in the order's direction, then a skip.
    assert.match(e.message, /filter speed=lte\.<the last speed a page gave>, start offset again at 0 and skip/);
    assert.ok(e.message.endsWith('ORDER BY "speed" DESC NULLS LAST, "lga" ASC NULLS FIRST, "year" ASC NULLS LAST, "lga" ASC NULLS LAST, file_row_number LIMIT 2 OFFSET 4'), e.message);
    return true;
  });
  assert.equal(reads.length, 0);
  await assert.rejects(parquetRows(env, entry, new URLSearchParams('order=speed.asc&limit=2&offset=4'), URL_, small), /filter speed=gte\./);
  // The heap never holds more than can match, so a page past the end answers empty, as D1 does.
  const past = new URLSearchParams('year=eq.2019&order=speed.desc&offset=100&limit=1');
  const end = await parquetRows(env, entry, past, URL_, { ...BUDGET, held: 8 });
  assert.deepEqual(end.rows, []);
  assert.equal(end.used.held, 8);
  assert.deepEqual((await d1('rows', past.toString(), SLUG3)).rows, []);
  // Without an order, a page is found by counting, so nothing is held.
  assert.equal((await parquetRows(env, entry, new URLSearchParams('limit=2&offset=30'), URL_, small)).used.held, 0);

  // The review's case: a deep offset into a twenty-million-row file is refused before it is read.
  const b = fixture('large-profiled.parquet');
  const big = { DIST: { async get(key, o = {}) {
    const r = o.range || {}, s = r.suffix !== undefined ? b.length - r.suffix : r.offset, e = r.suffix !== undefined ? b.length : s + r.length;
    const u = b.subarray(s, e);
    return { size: b.length, arrayBuffer: async () => u.buffer.slice(u.byteOffset, u.byteOffset + u.length) };
  } } };
  const large = await open(big, 'big', '2026-01-01');
  await assert.rejects(parquetRows(big, large, new URLSearchParams('n=lt.3900000&order=n.desc&offset=3899000&limit=2'), 'u'), (e) => {
    assert.match(e.message, /3,899,003 ordered rows/);
    return true;
  });
});

test('an ordered page is charged for the depth of its heap, and an aggregate for its buckets', async () => {
  // Each candidate row is compared once per level of the heap, so a deep page costs more values.
  const b = fixture('large-profiled.parquet');
  const big = { DIST: { async get(key, o = {}) {
    const r = o.range || {}, s = r.suffix !== undefined ? b.length - r.suffix : r.offset, e = r.suffix !== undefined ? b.length : s + r.length;
    const u = b.subarray(s, e);
    return { size: b.length, arrayBuffer: async () => u.buffer.slice(u.byteOffset, u.byteOffset + u.length) };
  } } };
  const large = await open(big, 'big', '2026-01-02');
  const values = async (offset) => {
    let msg = '';
    await assert.rejects(parquetRows(big, large, new URLSearchParams(`order=n.desc&limit=1&offset=${offset}`), 'u', { ...BUDGET, values: 1 }), (e) => { msg = e.message; return true; });
    return msg.match(/read ([\d,]+) values/)[1];
  };
  assert.equal(await values(0), '20,000,000');
  assert.equal(await values(300), '40,000,000');
  assert.equal(await values(70000), '60,000,000');

  // Every distinct group is a bucket in memory, so their number is capped and the SQL given.
  const qs = new URLSearchParams('group=lga&metric=count');
  await assert.rejects(parquetAggregate(env, entry, qs, URL_, { ...BUDGET, buckets: 3 }), (e) => {
    assert.ok(e instanceof BudgetError);
    assert.match(e.message, /4 distinct groups .*\(3 distinct groups\)/);
    assert.match(e.message, /Group by fewer or coarser fields/);
    assert.ok(e.message.endsWith(`SELECT "lga" AS "lga", COUNT(*) AS "count" FROM read_parquet('${URL_}', file_row_number = true) GROUP BY "lga" ORDER BY "lga" ASC NULLS FIRST LIMIT 100 OFFSET 0`), e.message);
    return true;
  });
  const ok = await parquetAggregate(env, entry, qs, URL_, { ...BUDGET, buckets: 5 });
  assert.equal(ok.used.buckets, 5);
  assert.deepEqual(ok.rows, (await d1('aggregate', qs.toString(), SLUG3)).rows);
});

test('a like pattern is charged for its length on every value it is matched against', async () => {
  const used = async (qs) => (await parquetAggregate(env, entry, new URLSearchParams(qs), URL_)).used.values;
  // A pattern without * matches as plain text and costs what any filter costs.
  const base = await used('lga=like.Nowhere&year=eq.2019');
  assert.ok(base > 0);
  // A three-character pattern counts each lga value twice; a hundred characters, fourteen times.
  const lga = (await used('lga=like.*o*&year=eq.2019')) - base;
  assert.ok(lga > 0);
  const long = '*o'.repeat(50);
  assert.equal(await used(`lga=like.${long}&year=eq.2019`), base + 13 * lga);
  await assert.rejects(parquetAggregate(env, entry, new URLSearchParams(`lga=like.${long}`), URL_, { ...BUDGET, values: 100 }), /values/);
});

test('the internal copy answers only while it matches the published file, and a failed read is not kept', async () => {
  const V = '2022-02-02';
  // A copy of another version, or of a different number of rows, is passed over for the published file.
  put(V, plain);
  objects.set(`_q/${SLUG}/${V}.parquet`, fixture('large-profiled.parquet'));
  assert.equal((await open({ DIST }, SLUG, V)).key, `d/${SLUG}/v/${V}/data.parquet`);
  const W = '2022-03-03';
  put(W, plain);
  objects.set(`_q/${SLUG}/${W}.parquet`, Buffer.from('not parquet at all'));
  assert.equal((await open({ DIST }, SLUG, W)).key, `d/${SLUG}/v/${W}/data.parquet`);

  // A transient R2 failure on the copy fails the call, and the next call reads the copy.
  const X = '2022-04-04';
  put(X, plain);
  putQ(X);
  let fail = true;
  const flaky = { DIST: { get: (key, o) => (fail && key.startsWith('_q/') ? Promise.reject(new Error('R2 503')) : DIST.get(key, o)) } };
  await assert.rejects(open(flaky, SLUG, X), /R2 503/);
  fail = false;
  assert.equal((await open(flaky, SLUG, X)).key, `_q/${SLUG}/${X}.parquet`);
});

test('a query copy written again in place is read afresh, and no answer from its old bytes is served', async () => {
  const V = '2021-05-05';
  put(V, plain);
  putQ(V, bytes);
  const cache = new Map();
  const was = globalThis.caches;
  globalThis.caches = {
    default: {
      match: async (k) => (cache.has(k.url) ? new Response(await cache.get(k.url)) : undefined),
      put: async (k, r) => { cache.set(k.url, r.text()); },
    },
  };
  const recheck = engine.RECHECK && engine.RECHECK.ms;
  try {
    const ask = { slug: SLUG, version: V, select: ['lga', 'year'], limit: 40 };
    const lgas = (r) => r.rows.map((x) => x.lga);
    const sorted = (await rowsOf(bytes)).map((r) => r.lga);
    const publisher = (await rowsOf(unsortedBytes)).map((r) => r.lga);
    assert.notDeepEqual(sorted, publisher);
    assert.deepEqual(lgas(await call('query_rows', ask)), sorted);
    // A layout edit writes the copy again under the same key, in the publisher's order.
    putQ(V, unsortedBytes);
    // A query not yet answered reads the new copy, though the isolate holds the old footer.
    const count = await call('count_rows', { slug: SLUG, version: V, where: { lga: 'Logan' } });
    assert.equal(count.error, undefined);
    assert.equal(count.groups[0].count, 8);
    // The answer cached from the old copy is not served for the new one.
    assert.deepEqual(lgas(await call('query_rows', ask)), publisher);
    // An isolate that reads nothing still notices the copy written again once its footer is old.
    putQ(V, bytes);
    if (engine.RECHECK) engine.RECHECK.ms = 0;
    assert.deepEqual(lgas(await call('query_rows', ask)), sorted);
  } finally {
    globalThis.caches = was;
    if (engine.RECHECK) engine.RECHECK.ms = recheck;
  }
});

test('a version stored only as period parts is located from its manifest, under the folder that wrote each part', async () => {
  const V = '2020-06-30', EARLIER = '2020-03-31';
  const part = (period, tree) => ({
    period, rows: 8, tree, finished: true, revised: false,
    files: { parquet: { path: `parts/${period}.parquet`, bytes: 1 }, 'csv.gz': { path: `parts/${period}.csv.gz`, bytes: 1 } },
  });
  // A finished part can be the file an earlier snapshot wrote, under that snapshot's folder.
  objects.set(`d/${SLUG}/v/${V}/manifest.json`, Buffer.from(JSON.stringify({ version: V, whole: false, period: { field: 'year', grain: 'year' }, parts: [part('2019', EARLIER), part('2020', V)] })));
  const at = await openVersion({ DIST }, SLUG, V);
  assert.deepEqual(at.files, []);
  assert.deepEqual(at.parts.map((p) => p.key), [`d/${SLUG}/v/${EARLIER}/parts/2019.parquet`, `d/${SLUG}/v/${V}/parts/2020.parquet`]);
  assert.deepEqual([at.rows, at.period], [16, { field: 'year', grain: 'year' }]);
  // Parts the manifest lists but R2 lacks fail the call, which names where the files are.
  const e = (await call('query_rows', { slug: SLUG, version: V, where: { year: 2019 } })).error;
  assert.match(e, new RegExp(`^version ${V} of ${SLUG} could not be read; its files are at`));
  assert.doesNotMatch(e, /_q/);
  // A version with neither a file nor parts still has none.
  assert.match((await call('query_rows', { slug: SLUG, version: '2020-07-31' })).error, /has no version 2020-07-31/);
});
