import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync } from 'node:fs';
import { gzipSync } from 'node:zlib';
import { DatabaseSync } from 'node:sqlite';
import { test } from 'node:test';
import { parquetReadObjects } from 'hyparquet';
import { decompress } from 'fzstd';
import { answer } from './_api.js';
import {
  BUDGET,
  BudgetError,
  RECHECK,
  forget,
  openVersion,
  parquetAggregate,
  parquetRows,
  periodStat,
} from './_parquet.js';
import { onRequestPost } from './mcp.js';

// Versions stored as period parts, written by the pipeline's part writer under the profile
// (pipeline/tests/test_parts_fixture.py). crashes-by-year is split on an integer year, sorted by
// place and day with a page index, and its newest version takes three parts from the version
// before and revises the fourth. crashes-by-quarter is split on a date by quarter, in the publisher's order with no page
// index, and has an undated part. D1 holds each one's rows in the order the engine defines: the
// parts in the manifest's order, then each part's own order.
const ROOT = new URL('../pipeline/tests/fixtures/parts/', import.meta.url);
const V = '2026-04-01',
  HELD = '2026-05-01';
const BY_YEAR = 'crashes-by-year',
  BY_QUARTER = 'crashes-by-quarter';
const SITE = 'https://publicdata.au';

const objects = new Map();
for (const rel of readdirSync(ROOT, { recursive: true })) {
  if (/\.(parquet|json)$/.test(rel)) {
    objects.set(`d/${rel.split('\\').join('/')}`, readFileSync(new URL(rel, ROOT)));
  }
}
const reads = [];
const etagOf = (b) => createHash('md5').update(b).digest('hex');
const DIST = {
  async get(key, opts = {}) {
    const b = objects.get(key);
    if (!b) {
      return null;
    }
    const etag = etagOf(b);
    const want = opts.onlyIf && opts.onlyIf.etagMatches;
    if (want !== undefined && want !== etag) {
      return { size: b.length, etag };
    }
    const r = opts.range || {};
    const start = r.suffix !== undefined ? Math.max(0, b.length - r.suffix) : r.offset || 0;
    const end =
      r.suffix !== undefined ? b.length : r.length !== undefined ? start + r.length : b.length;
    reads.push({ key, start, end });
    const out = b.subarray(start, end);
    return {
      size: b.length,
      etag,
      arrayBuffer: async () => out.buffer.slice(out.byteOffset, out.byteOffset + out.length),
      text: async () => Buffer.from(out).toString('utf8'),
    };
  },
  async head(key) {
    const b = objects.get(key);
    return b ? { size: b.length, etag: etagOf(b) } : null;
  },
};

const manifestOf = (slug) => JSON.parse(objects.get(`d/${slug}/v/${V}/manifest.json`));
const partKey = (slug, p) => `d/${slug}/v/${p.tree}/${p.files.parquet.path}`;
const rowsOf = (b) =>
  parquetReadObjects({
    file: b.buffer.slice(b.byteOffset, b.byteOffset + b.length),
    compressors: { ZSTD: (i, n) => decompress(i, new Uint8Array(n)) },
  });

const D1_FIELDS = [
  { name: 'lga', type: 'string' },
  { name: 'year', type: 'integer' },
  { name: 'fatal', type: 'boolean' },
  { name: 'speed', type: 'number' },
  { name: 'day', type: 'date' },
  { name: 'seen', type: 'datetime' },
  { name: 'ref', type: 'integer' },
  { name: 'suppressed', type: 'array' },
];
const sql = new DatabaseSync(':memory:');
sql.exec(
  'CREATE TABLE _versions (slug TEXT, version TEXT, tbl TEXT, fields TEXT, rows INTEGER, attribution TEXT, header TEXT)',
);
async function load(slug, tbl) {
  const all = [];
  for (const p of manifestOf(slug).parts) {
    all.push(...(await rowsOf(objects.get(partKey(slug, p)))));
  }
  sql.exec(
    `CREATE TABLE ${tbl} (lga TEXT, year INTEGER, fatal INTEGER, speed REAL, day TEXT, seen TEXT, ref REAL, suppressed TEXT)`,
  );
  sql
    .prepare('INSERT INTO _versions VALUES (?, ?, ?, ?, ?, ?, ?)')
    .run(slug, HELD, tbl, JSON.stringify(D1_FIELDS), all.length, 'x', '{}');
  const ins = sql.prepare(`INSERT INTO ${tbl} VALUES (?, ?, ?, ?, ?, ?, ?, ?)`);
  for (const r of all) {
    ins.run(
      r.lga,
      Number(r.year),
      r.fatal === null ? null : r.fatal ? 1 : 0,
      r.speed,
      r.day && r.day.toISOString().slice(0, 10),
      r.seen && r.seen.toISOString().slice(0, 19),
      Number(r.ref),
      r.suppressed && r.suppressed.length ? r.suppressed.join(';') : null,
    );
  }
  return all.length;
}
await load(BY_YEAR, 'ty');
await load(BY_QUARTER, 'tq');
const DB = {
  prepare(q) {
    let binds = [];
    const st = {
      bind(...b) {
        binds = b;
        return st;
      },
      async first() {
        const r = sql.prepare(q).get(...binds);
        return r ? { ...r } : null;
      },
      async all() {
        return {
          results: sql
            .prepare(q)
            .all(...binds)
            .map((r) => ({ ...r })),
        };
      },
    };
    return st;
  },
};
globalThis.caches = { default: { match: async () => undefined, put: async () => {} } };
const assets = { '/latest.json': { [BY_YEAR]: HELD, [BY_QUARTER]: HELD } };
const env = {
  DB,
  DIST,
  ASSETS: {
    fetch: async (r) => {
      const p = new URL(r.url || r).pathname;
      return p in assets ? Response.json(assets[p]) : new Response('', { status: 404 });
    },
  },
};

async function d1(op, qs, slug) {
  const request = new Request(`${SITE}/api/v1/datasets/${slug}/${op}?${qs}`);
  const r = await answer({ request, env, params: { slug }, waitUntil() {} }, op);
  const b = await r.json();
  assert.equal(r.status, 200, `${qs}: ${b.error} ${b.detail}`);
  return b;
}
// D1 holds ref as REAL, so the one value above 2**53 is compared on its own elsewhere.
const noRef = (rows) =>
  rows.map((r) => Object.fromEntries(Object.entries(r).filter(([k]) => k !== 'ref')));
const manifestUrl = (slug) => `${SITE}/d/${slug}/v/${V}/manifest.json`;
const at = {
  [BY_YEAR]: await openVersion({ DIST }, BY_YEAR, V),
  [BY_QUARTER]: await openVersion({ DIST }, BY_QUARTER, V),
};

// Each case with the parts a filter on the period field leaves, by slug; null means every part.
const ROWS = {
  [BY_YEAR]: [
    ['', null],
    ['year=eq.2019', 1],
    ['year=gte.2021', 2],
    ['year=gt.2021', 1],
    ['year=lte.2018', 1],
    ['year=lt.2018', 0],
    ['year=in.(2018,2022)', 2],
    ['year=neq.2020', 4],
    ['year=not.eq.2020', null],
    ['year=is.null', 0],
    ['year=not.is.null', null],
    ['year=eq.2020&lga=eq.Logan', 1],
    ['year=gte.2019&year=lt.2021&order=day.desc', 2],
    ['lga=eq.Logan', null],
    ['lga=is.null', null],
    ['lga=in.(Brisbane,Cairns)&year=neq.2022', 4],
    ['lga=ilike.*coast*', null],
    ['lga=like.É*', null],
    ['fatal=eq.true&year=lte.2019', 2],
    ['speed=gt.55&order=speed.desc,lga.asc', null],
    ['order=lga.asc,year.desc&limit=7&offset=3', null],
    ['limit=7&offset=26', null],
    ['limit=40&offset=55', null],
    ['order=day.desc&limit=12&offset=5', null],
    ['day=gte.2020-06-01', null],
    ['day=is.null', null],
    ['seen=lt.2026-04-24T10:00:00&order=seen.desc&limit=9', null],
    ['select=year,lga&limit=3&offset=58', null],
    ['year=eq.2030', 0],
    ['select=suppressed,lga&suppressed=like.*speed*', null],
    ['year=in.(2019,2021)&order=ref.asc&select=ref,year&limit=50', 2],
    ['year=gte.2019&limit=5&offset=100', 4],
  ],
  [BY_QUARTER]: [
    ['', null],
    ['day=gte.2020-04-01&day=lt.2020-10-01', 2],
    ['day=eq.2019-02-04', 1],
    ['day=is.null', 1],
    ['day=not.is.null', 20],
    ['day=gt.2022-09-30', 1],
    ['day=gte.2022-09-30', 2],
    ['day=gt.2022-12-31', 0],
    ['day=lte.2018-03-31', 1],
    ['day=lt.2018-04-01', 1],
    ['day=in.(2018-05-06,2021-12-01)', 2],
    ['day=neq.2020-01-01', 20],
    ['day=like.2019*', 20],
    ['year=eq.2019', null],
    ['lga=eq.Logan&day=gte.2021-01-01', 8],
    ['order=day.asc&limit=10&offset=20', null],
    ['order=day.desc,lga.asc&limit=25', null],
    ['limit=11&offset=13', null],
    ['limit=30&offset=130', null],
    ['speed=lt.50&order=speed.asc,ref.desc&limit=20', null],
    ['day=gte.2019-07-01&day=lte.2019-12-31&select=day,lga', 2],
    ['seen=not.is.null&day=is.null', 1],
  ],
};

test('rows match the query API answer across parts for every filter, order and page', async () => {
  for (const slug of [BY_YEAR, BY_QUARTER]) {
    const total = at[slug].parts.length;
    for (const [qs, kept] of ROWS[slug]) {
      const want = await d1('rows', qs, slug);
      const got = await parquetRows(env, at[slug], new URLSearchParams(qs), manifestUrl(slug));
      assert.deepEqual(noRef(got.rows), noRef(want.rows), `${slug} ${qs}`);
      assert.equal(got.more, want.next !== null, `${slug} ${qs}`);
      const count = await d1(
        'aggregate',
        qs
          .split('&')
          .filter((p) => p && !/^(order|limit|offset|select)=/.test(p))
          .join('&'),
        slug,
      );
      assert.equal(got.matched, count.rows[0].count, `${slug} ${qs}`);
      assert.equal(got.used.parts, kept ?? total, `${slug} ${qs}: parts read`);
      assert.equal(got.parts.length, got.used.parts);
    }
  }
});

test('aggregates combine across parts as the query API answers them', async () => {
  const cases = [
    'group=lga&metric=count&order=count.desc,lga.asc',
    'group=year&metric=sum.speed,avg.speed,min.day,max.lga,count.fatal',
    'metric=count&year=eq.2030',
    'metric=sum.fatal,min.speed,max.seen',
    'group=fatal,year&metric=count&limit=4&offset=2',
    'group=lga&metric=sum.fatal&lga=not.is.null&order=sum_fatal.desc,lga.asc&limit=2',
    'group=year&metric=count&year=gte.2021',
    'group=suppressed&metric=count,sum.speed&order=count.desc,suppressed.asc',
    'metric=count&day=is.null',
    'group=lga&metric=max.day,min.seen&day=gte.2020-01-01',
    'metric=count',
    'metric=count&lga=eq.Logan',
    'group=year&metric=avg.speed&day=lt.2019-07-01',
  ];
  for (const slug of [BY_YEAR, BY_QUARTER]) {
    for (const qs of cases) {
      const want = await d1('aggregate', qs, slug);
      const got = await parquetAggregate(env, at[slug], new URLSearchParams(qs), manifestUrl(slug));
      assert.deepEqual(got.rows, want.rows, `${slug} ${qs}`);
      assert.equal(got.more, want.next !== null, `${slug} ${qs}`);
    }
  }
});

test('a part a filter on the period field rules out is never read, not even its footer', async () => {
  const keys = (slug) => at[slug].parts.map((p) => p.key);
  reads.length = 0;
  const q = await parquetAggregate(
    env,
    at[BY_QUARTER],
    new URLSearchParams('day=gte.2020-04-01&day=lt.2020-10-01&group=lga'),
    manifestUrl(BY_QUARTER),
  );
  assert.deepEqual(q.parts, ['2020-Q2', '2020-Q3']);
  const touched = new Set(reads.map((r) => r.key));
  // The newest part's footer gives the fields, read once per isolate; beyond it only the two
  // quarters are read.
  const allowed = ['2020-Q2', '2020-Q3', 'undated'].map(
    (p) => `d/${BY_QUARTER}/v/${V}/parts/${p}.parquet`,
  );
  assert.ok(
    [...touched].every((k) => allowed.includes(k)),
    [...touched].join(' '),
  );
  assert.ok(touched.has(allowed[0]) && touched.has(allowed[1]));
  assert.ok(keys(BY_QUARTER).length > 20);
  // A part the version took from the version before is read from that version's folder.
  const y = await parquetRows(
    env,
    at[BY_YEAR],
    new URLSearchParams('year=eq.2020&select=year&limit=1'),
    manifestUrl(BY_YEAR),
  );
  assert.deepEqual([y.parts, y.rows], [['2020'], [{ year: 2020 }]]);
  assert.equal(at[BY_YEAR].parts[2].key, `d/${BY_YEAR}/v/2026-03-01/parts/2020.parquet`);
  // The part the newer version revised is its own, so the URLs do not sort in period order.
  assert.equal(at[BY_YEAR].parts[1].key, `d/${BY_YEAR}/v/${V}/parts/2019.parquet`);
  // A count every statistic proves reads no data page in any part.
  const all = await parquetAggregate(
    env,
    at[BY_YEAR],
    new URLSearchParams('year=gte.2018'),
    manifestUrl(BY_YEAR),
  );
  assert.deepEqual([all.rows[0].count, all.used.groups, all.used.parts], [150, 0, 5]);
});

test('every range a call reads across parts is charged to its budget, with near ranges of one part read as one', async () => {
  // An ordered page reads its plan in one go. (An unordered page and an aggregate read a few
  // groups at a time, so their reads can split a run the plan charged as one.)
  const cases = [
    [BY_YEAR, 'lga=eq.Logan&order=day.desc&limit=20'],
    [BY_YEAR, 'order=speed.desc,lga.asc&select=lga,speed,seen&limit=40'],
    [BY_QUARTER, 'speed=gt.45&order=day.asc&select=lga,day,suppressed&limit=200'],
    [BY_QUARTER, 'order=seen.desc&limit=60'],
  ];
  for (const [slug, qs] of cases) {
    const fn = parquetRows;
    // The footers and page index are read once; what is left is the data the budget counts.
    await fn(env, at[slug], new URLSearchParams(qs), manifestUrl(slug));
    reads.length = 0;
    const r = await fn(env, at[slug], new URLSearchParams(qs), manifestUrl(slug));
    assert.ok(r.used.ranges > 0, qs);
    assert.equal(reads.length, r.used.ranges, `${slug} ${qs}: ${JSON.stringify(reads)}`);
  }
});

test("a part's page index comes with its footer in one read, when the tail holds both", async () => {
  forget(BY_YEAR, V);
  reads.length = 0;
  const r = await parquetAggregate(
    env,
    at[BY_YEAR],
    new URLSearchParams('year=eq.2020&lga=eq.Logan&group=day'),
    manifestUrl(BY_YEAR),
  );
  const key = `d/${BY_YEAR}/v/2026-03-01/parts/2020.parquet`;
  assert.ok(r.used.ranges > 0);
  // One read of the tail for the footer and the page index, then only the data the budget counts.
  assert.equal(reads.filter((x) => x.key === key).length, 1 + r.used.ranges, JSON.stringify(reads));
});

test('the period bounds of a part are never narrower than its rows', () => {
  assert.deepEqual(periodStat('2019', 'year', 'integer', 3), {
    min: 2019,
    max: 2019,
    nulls: 0,
    rows: 3,
  });
  assert.deepEqual(periodStat('2019-Q4', 'quarter', 'date', 3), {
    min: '2019-10-01',
    max: '2019-12-31',
    nulls: 0,
    rows: 3,
  });
  assert.deepEqual(periodStat('2020-02', 'month', 'date', 3), {
    min: '2020-02-01',
    max: '2020-02-29',
    nulls: 0,
    rows: 3,
  });
  assert.deepEqual(periodStat('2019-20', 'fiscal', 'datetime', 3), {
    min: '2019-07-01T00:00:00',
    max: '2020-06-30T23:59:59',
    nulls: 0,
    rows: 3,
  });
  assert.deepEqual(periodStat('2019', 'year', 'date', 3), {
    min: '2019-01-01',
    max: '2019-12-31',
    nulls: 0,
    rows: 3,
  });
  assert.deepEqual(periodStat('undated', 'year', 'date', 3), {
    min: undefined,
    max: undefined,
    nulls: 3,
    rows: 3,
  });
  // A label the reader cannot place, or a field it cannot bound, rules nothing out.
  assert.equal(periodStat('2019-Q1', 'quarter', 'integer', 3), null);
  assert.equal(periodStat('2019', 'year', 'string', 3), null);
  assert.equal(periodStat('spring', 'year', 'date', 3), null);
});

const urls = (slug, periods) =>
  `[${periods
    .map((p) => {
      const part = at[slug].parts.find((x) => x.period === p);
      return `'${SITE}/${part.key}'`;
    })
    .join(', ')}]`;

test('a call over the budget is refused with DuckDB SQL over the parts it would read', async () => {
  const qs = 'year=gte.2020&lga=eq.Logan&order=day.desc&select=lga,day&limit=5';
  const list = urls(BY_YEAR, ['2020', '2021', '2022']);
  const want = `SELECT "lga" AS "lga", "day" AS "day" FROM read_parquet(${list}, filename = true, file_row_number = true, union_by_name = true) WHERE "year" >= 2020 AND "lga" = 'Logan' ORDER BY "day" DESC NULLS LAST, list_position(${list}, filename), file_row_number LIMIT 5 OFFSET 0`;
  await assert.rejects(
    parquetRows(env, at[BY_YEAR], new URLSearchParams(qs), manifestUrl(BY_YEAR), {
      ...BUDGET,
      groups: 2,
    }),
    (e) => {
      assert.ok(e instanceof BudgetError);
      assert.match(e.message, /would read \d+ row groups of the version's 150 rows/);
      assert.ok(
        e.message.includes(
          `download the 3 parts its manifest at ${manifestUrl(BY_YEAR)} lists for those periods, or run this DuckDB SQL, which reads those parts directly: `,
        ),
        e.message,
      );
      assert.ok(e.message.endsWith(want), e.message);
      return true;
    },
  );
  // More parts than one call may open is refused before any of their footers is read.
  forget(BY_QUARTER, V);
  reads.length = 0;
  await assert.rejects(
    parquetAggregate(
      env,
      at[BY_QUARTER],
      new URLSearchParams('group=lga&day=gte.2021-01-01'),
      manifestUrl(BY_QUARTER),
      { ...BUDGET, parts: 7 },
    ),
    (e) => {
      assert.match(
        e.message,
        /would read 8 period parts .*\(7 period parts\)\. Narrow where on day to fewer periods\./,
      );
      const list8 = urls(BY_QUARTER, [
        '2021-Q1',
        '2021-Q2',
        '2021-Q3',
        '2021-Q4',
        '2022-Q1',
        '2022-Q2',
        '2022-Q3',
        '2022-Q4',
      ]);
      assert.ok(
        e.message.endsWith(
          `SELECT "lga" AS "lga", COUNT(*) AS "count" FROM read_parquet(${list8}, filename = true, file_row_number = true, union_by_name = true) WHERE "day" >= '2021-01-01' GROUP BY "lga" ORDER BY "lga" ASC NULLS FIRST LIMIT 100 OFFSET 0`,
        ),
        e.message,
      );
      return true;
    },
  );
  assert.deepEqual(
    reads.map((r) => r.key),
    [`d/${BY_QUARTER}/v/${V}/parts/undated.parquet`],
  );
  // Within budget, both answer.
  assert.ok(
    (await parquetRows(env, at[BY_YEAR], new URLSearchParams(qs), manifestUrl(BY_YEAR))).rows
      .length,
  );
});

let id = 0;
async function call(name, args) {
  const request = new Request(`${SITE}/mcp`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: ++id,
      method: 'tools/call',
      params: { name, arguments: args },
    }),
  });
  const r = await (await onRequestPost({ request, env, waitUntil() {} })).json();
  return r.result.isError ? { error: r.result.content[0].text } : r.result.structuredContent;
}

test('the row tools answer a version stored as parts, naming its manifest, attribution and the periods read', async () => {
  const r = await call('query_rows', {
    slug: BY_QUARTER,
    version: V,
    where: { day: { min: '2019-01-01', max: '2019-06-30' } },
    select: ['day'],
    order: 'day',
    limit: 3,
  });
  assert.equal(r.error, undefined, r.error);
  assert.deepEqual(
    r.rows,
    (
      await d1(
        'rows',
        'day=gte.2019-01-01&day=lte.2019-06-30&select=day&order=day&limit=3',
        BY_QUARTER,
      )
    ).rows,
  );
  assert.equal(r.rows.length, 3);
  assert.equal(r.version, V);
  assert.deepEqual(r.parts, ['2019-Q1', '2019-Q2']);
  assert.equal(r.manifest, manifestUrl(BY_QUARTER));
  assert.equal(r.attribution, 'Fixture publisher, licensed under CC BY 4.0.');
  assert.equal(r.file, undefined);
  assert.equal(
    r.matched,
    (await d1('aggregate', 'day=gte.2019-01-01&day=lte.2019-06-30', BY_QUARTER)).rows[0].count,
  );
  const c = await call('count_rows', {
    slug: BY_YEAR,
    version: V,
    group_by: ['year'],
    where: { lga: 'Logan' },
  });
  // Ties in the count fall to the group's own order, which the query API leaves to SQLite.
  assert.deepEqual(
    c.groups,
    (
      await d1(
        'aggregate',
        'lga=eq.Logan&group=year&metric=count&order=count.desc,year.asc',
        BY_YEAR,
      )
    ).rows,
  );
  assert.equal(c.parts.length, 5);
  assert.match(
    (await call('count_rows', { slug: BY_YEAR, version: V, where: { nope: 1 } })).error,
    /no field nope/,
  );
});

test('a part written again in place is read afresh, and no answer from its old footer is served', async () => {
  const key = `d/${BY_YEAR}/v/${V}/parts/2022.parquet`;
  const was = objects.get(key);
  const ask = { slug: BY_YEAR, version: V, where: { year: 2022 }, select: ['lga'], limit: 50 };
  assert.equal((await call('query_rows', ask)).matched, 30);
  // Another part's bytes stand in for a rewrite. The footer the isolate holds says every row is
  // 2022; the first data read fails its ETag, and the part read afresh holds no 2022 rows.
  objects.set(key, objects.get(`d/${BY_YEAR}/v/2026-03-01/parts/2021.parquet`));
  try {
    const after = await call('query_rows', ask);
    assert.equal(after.error, undefined, after.error);
    assert.deepEqual([after.matched, after.rows], [0, []]);
  } finally {
    objects.set(key, was);
  }
});

test('a manifest R2 stores gzipped is read as JSON, so its parts are found', async () => {
  const key = `d/${BY_QUARTER}/v/${V}/manifest.json`;
  const raw = objects.get(key);
  const gz = gzipSync(raw);
  assert.ok(raw.length > 1024);
  // The binding returns the stored bytes; only the metadata says they are gzipped.
  const zipped = {
    ...DIST,
    async get(k, opts) {
      if (k !== key) {
        return DIST.get(k, opts);
      }
      return {
        size: gz.length,
        etag: etagOf(gz),
        httpMetadata: { contentEncoding: 'gzip' },
        body: new Blob([gz]).stream(),
        text: async () => gz.toString('utf8'),
      };
    },
  };
  forget(BY_QUARTER, V);
  try {
    const got = await openVersion({ DIST: zipped }, BY_QUARTER, V);
    assert.ok(got, 'the version is located');
    assert.deepEqual(
      got.parts.map((p) => p.key),
      at[BY_QUARTER].parts.map((p) => p.key),
    );
    assert.equal(got.manifest.sha256, createHash('sha256').update(raw).digest('hex'));
    const r = await parquetRows(
      { DIST: zipped },
      got,
      new URLSearchParams('day=eq.2019-02-04'),
      manifestUrl(BY_QUARTER),
    );
    assert.equal(r.matched, (await d1('aggregate', 'day=eq.2019-02-04', BY_QUARTER)).rows[0].count);
  } finally {
    forget(BY_QUARTER, V);
  }
});

test('an answer from parts is not served once a correction rewrites the manifest', async () => {
  const key = `d/${BY_YEAR}/v/${V}/manifest.json`;
  const was = objects.get(key);
  const store = new Map();
  const before = globalThis.caches;
  globalThis.caches = {
    default: {
      match: async (r) => store.get(r.url)?.clone(),
      put: async (r, res) => {
        store.set(r.url, res);
      },
    },
  };
  const ask = { slug: BY_YEAR, version: V, where: { year: 2020 }, select: ['lga'], limit: 5 };
  const answers = () => [...store.keys()].filter((k) => k.includes('/_parquet/')).length;
  const recheck = RECHECK.ms;
  try {
    await call('query_rows', ask);
    assert.equal(answers(), 1);
    await call('query_rows', ask);
    assert.equal(answers(), 1, 'an unchanged manifest answers from the cache');
    // A correction keeps every part's key and rows and adds its note to the manifest.
    objects.set(key, Buffer.from(JSON.stringify({ ...JSON.parse(was), notes: 'Corrected.' })));
    // The isolate notices the manifest's new ETag once its footer is checked again.
    RECHECK.ms = 0;
    await call('query_rows', ask);
    assert.equal(answers(), 2, 'the corrected version is read afresh under a key of its own');
  } finally {
    RECHECK.ms = recheck;
    objects.set(key, was);
    globalThis.caches = before;
    forget(BY_YEAR, V);
  }
});
