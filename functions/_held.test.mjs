import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { test } from 'node:test';
import { gunzipSync } from 'node:zlib';
import { answer } from './_api.js';
import { aggregateQuery, rowsQuery } from './_query.js';

// A version D1 holds answers through the one query path exactly as D1 always answered it: the rows
// the query builder's own SQL gives, under the header D1 holds, even where the version has a
// current rollup. The rows and rollup are the fixture the rollup tests read
// (pipeline/tests/fixtures/rollup).
const FIX = new URL('../pipeline/tests/fixtures/rollup/', import.meta.url);
const data = JSON.parse(readFileSync(new URL('rows.json', FIX), 'utf8'));
const gz = readFileSync(new URL('rollup.json.gz', FIX));
const R = JSON.parse(gunzipSync(gz).toString('utf8'));
const SLUG = 'held',
  V = R.version,
  SITE = 'https://publicdata.au';
const TYPES = { string: 'TEXT', integer: 'INTEGER', boolean: 'INTEGER', date: 'TEXT' };
const sql = new DatabaseSync(':memory:');
sql.exec(
  'CREATE TABLE _versions (slug TEXT, version TEXT, tbl TEXT, fields TEXT, rows INTEGER, attribution TEXT, header TEXT)',
);
const header = {
  dataset: SLUG,
  version: V,
  publisher: { name: 'Held Agency' },
  licence: { id: 'CC-BY-4.0' },
  attribution: 'Held Agency, CC BY 4.0.',
  source: {
    url: 'https://example.gov.au/held.csv',
    fetched_at: '2026-01-01T00:00:00Z',
    sha256: 'd1d1',
  },
};
function table(tbl, rows) {
  sql.exec(`DROP TABLE IF EXISTS ${tbl}`);
  sql.exec(
    `CREATE TABLE ${tbl} (${data.fields.map((f) => `"${f.name}" ${TYPES[f.type]}`).join(', ')})`,
  );
  const ins = sql.prepare(`INSERT INTO ${tbl} VALUES (${data.fields.map(() => '?').join(', ')})`);
  for (const r of rows) {
    ins.run(...r);
  }
}
table('t_all', data.rows);
sql
  .prepare('INSERT INTO _versions VALUES (?, ?, ?, ?, ?, ?, ?)')
  .run(
    SLUG,
    V,
    't_all',
    JSON.stringify(data.fields),
    data.rows.length,
    header.attribution,
    JSON.stringify(header),
  );
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
// R2 holds the version's rollup, stamped with the identity of its published Parquet.
const ROLLUP = `_rollup/${SLUG}/${V}.json.gz`;
const DIST = {
  async get(k) {
    return k === ROLLUP
      ? { customMetadata: { parquet: 'sha256:p' }, etag: 'r', body: new Response(gz).body }
      : null;
  },
  async head(k) {
    if (k === ROLLUP) {
      return { customMetadata: { parquet: 'sha256:p' }, etag: 'r' };
    }
    return k === `d/${SLUG}/v/${V}/data.parquet`
      ? { customMetadata: { sha256: 'p' }, etag: 'e' }
      : null;
  },
};
const ASSETS = {
  fetch: async (r) =>
    new URL(r.url || r).pathname === '/latest.json'
      ? Response.json({ [SLUG]: V })
      : new Response('', { status: 404 }),
};
const env = { DB, DIST, ASSETS };
globalThis.caches = { default: { match: async () => undefined, put: async () => {} } };

async function api(op, qs) {
  const request = new Request(`${SITE}/api/v1/datasets/${SLUG}/versions/${V}/${op}?${qs}`);
  const r = await answer({ request, env, params: { slug: SLUG, version: V }, waitUntil() {} }, op);
  return {
    status: r.status,
    sha: r.headers.get('x-publicdata-source-sha256'),
    body: await r.json(),
  };
}
const viaD1 = (op, qs, tbl = 't_all') => {
  const plan = (op === 'rows' ? rowsQuery : aggregateQuery)(
    tbl,
    data.fields,
    new URLSearchParams(qs),
  );
  const rows = sql
    .prepare(plan.sql)
    .all(...plan.binds)
    .map((r) => ({ ...r }));
  return rows.length > plan.limit ? rows.slice(0, plan.limit) : rows;
};

// Seeded, so a failure names a query that fails again.
let seed = 5;
const rand = (n) => {
  seed = (seed * 1103515245 + 12345) % 2 ** 31;
  return seed % n;
};
const pick = (a) => a[rand(a.length)];
const vals = {
  lga: ['Gold Coast', 'Brisbane', "O'Connor", 'Nowhere'],
  year: ['2019', '2020', '2021', '2022'],
  severity: ['Fatal', 'Minor', 'Hospitalisation'],
  fatal: ['true', 'false'],
};
const filter = () => {
  const f = pick(Object.keys(vals));
  return rand(4)
    ? `${f}=${pick(['eq', 'neq', 'gte', 'lt'])}.${encodeURIComponent(pick(vals[f]))}`
    : `${f}=is.null`;
};

test('every answer for a version D1 holds is the one its SQL gives, under the header D1 holds', async () => {
  for (let i = 0; i < 400; i++) {
    const where = Array.from({ length: rand(3) }, filter);
    let op, qs;
    if (rand(2)) {
      const group = [
        ...new Set(
          Array.from({ length: rand(3) }, () => pick(['lga', 'year', 'severity', 'fatal'])),
        ),
      ];
      const metric = pick([
        'count',
        'sum.casualties',
        'avg.casualties',
        'min.speed',
        'max.speed',
        'sum.fatal',
      ]);
      const alias = metric === 'count' ? 'count' : metric.replace('.', '_');
      const order = pick([
        [],
        [`${alias}.desc`],
        [`${alias}.asc`, ...group.map((g) => `${g}.asc`)],
        group.map((g) => `${g}.desc`),
      ]);
      op = 'aggregate';
      qs = [
        ...where,
        `metric=${metric}`,
        ...(group.length ? [`group=${group.join(',')}`] : []),
        ...(order.length ? [`order=${order.join(',')}`] : []),
        `limit=${1 + rand(10)}`,
      ].join('&');
    } else {
      op = 'rows';
      qs = [
        ...where,
        ...(rand(2) ? [`order=${pick(['lga', 'year', 'speed'])}.${pick(['asc', 'desc'])}`] : []),
        `limit=${1 + rand(20)}`,
      ].join('&');
    }
    const a = await api(op, qs);
    assert.equal(a.status, 200, `${qs}: ${a.body.error}`);
    assert.deepEqual(a.body.rows, viaD1(op, qs), `${op} ${qs}`);
    assert.deepEqual(a.body.publicdata, header, qs);
    assert.equal(a.sha, 'd1d1', qs);
    assert.equal(a.body.file, undefined, qs);
    assert.equal(a.body.order, undefined, qs);
  }
});

test("the rollup answers ahead of D1 only an order that leaves no ties, under D1's header", async () => {
  // D1 holds fewer rows than the rollup was counted from, so each answer shows who gave it.
  table('t_less', data.rows.slice(0, 400));
  sql.prepare('UPDATE _versions SET tbl = ? WHERE slug = ?').run('t_less', SLUG);
  try {
    const tied = 'metric=count&group=lga&order=count.desc&limit=50';
    const a = await api('aggregate', tied);
    assert.deepEqual(a.body.rows, viaD1('aggregate', tied, 't_less'));
    const settled = 'metric=count&group=lga&order=count.desc,lga.asc&limit=50';
    const b = await api('aggregate', settled);
    assert.notDeepEqual(b.body.rows, viaD1('aggregate', settled, 't_less'));
    assert.deepEqual(b.body.publicdata, header);
    assert.equal(b.sha, 'd1d1');
    assert.equal(b.body.file, undefined);
  } finally {
    sql.prepare('UPDATE _versions SET tbl = ? WHERE slug = ?').run('t_all', SLUG);
  }
});
