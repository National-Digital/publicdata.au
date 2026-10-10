import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { test } from 'node:test';
import { QueryError, aggregateQuery, rowsQuery, toCSV } from './_query.js';

const fields = [
  { name: 'lga', type: 'string' },
  { name: 'year', type: 'integer' },
  { name: 'fatal', type: 'boolean' },
  { name: 'speed_limit', type: 'integer' },
];
const db = new DatabaseSync(':memory:');
db.exec('CREATE TABLE "v_t_20260424" (lga TEXT, year INTEGER, fatal INTEGER, speed_limit INTEGER)');
const ins = db.prepare('INSERT INTO "v_t_20260424" VALUES (?, ?, ?, ?)');
for (const r of [
  ['Gold Coast', 2020, 1, 60],
  ['Gold Coast', 2021, 0, null],
  ['Brisbane', 2020, 0, 60],
  ["O'Connor", 2021, 1, 100],
]) {
  ins.run(...r);
}
const run = (plan) =>
  db
    .prepare(plan.sql)
    .all(...plan.binds)
    .map((r) => ({ ...r }));
const P = (s) => new URLSearchParams(s);

test('filters bind their values and type them by field', () => {
  const plan = rowsQuery(
    'v_t_20260424',
    fields,
    P('lga=eq.Gold Coast&year=gte.2021&select=lga,year'),
  );
  assert.deepEqual(plan.binds, ['Gold Coast', 2021]);
  assert.deepEqual(run(plan), [{ lga: 'Gold Coast', year: 2021 }]);
  assert.equal(run(rowsQuery('v_t_20260424', fields, P("lga=eq.O'Connor"))).length, 1);
  assert.equal(run(rowsQuery('v_t_20260424', fields, P('fatal=eq.true'))).length, 2);
  assert.equal(
    run(rowsQuery('v_t_20260424', fields, P('lga=in.(Brisbane,Gold Coast)&year=eq.2020'))).length,
    2,
  );
  assert.equal(run(rowsQuery('v_t_20260424', fields, P('speed_limit=is.null'))).length, 1);
  assert.equal(run(rowsQuery('v_t_20260424', fields, P('speed_limit=not.is.null'))).length, 3);
  assert.equal(run(rowsQuery('v_t_20260424', fields, P('lga=ilike.*coast*'))).length, 2);
  db.prepare('INSERT INTO "v_t_20260424" VALUES (?, ?, ?, ?)').run('100%_rural', 2022, 0, 80);
  assert.deepEqual(run(rowsQuery('v_t_20260424', fields, P('lga=like.100%_*&select=lga'))), [
    { lga: '100%_rural' },
  ]);
  assert.equal(run(rowsQuery('v_t_20260424', fields, P('lga=like.Gold_Coast'))).length, 0);
  db.exec('DELETE FROM "v_t_20260424" WHERE lga = \'100%_rural\'');
});

test('names that are not fields never reach the SQL', () => {
  assert.throws(() => rowsQuery('t', fields, P('select=lga,"x";drop table t')), QueryError);
  assert.throws(() => rowsQuery('t', fields, P('nope=eq.1')), /no field nope/);
  assert.throws(() => rowsQuery('t', fields, P('order=year;drop')), QueryError);
  assert.throws(() => rowsQuery('t', fields, P('year=eq.abc')), /takes a number/);
  assert.throws(() => rowsQuery('t', fields, P('year=2020')), /needs an operator/);
  assert.throws(() => rowsQuery('t', fields, P('limit=100000')), /limit/);
  assert.throws(() => rowsQuery('t', fields, P('select=,')), /select names no fields/);
});

test('paging asks for one row more to know whether there is a next page', () => {
  const plan = rowsQuery('v_t_20260424', fields, P('order=year.asc,lga.asc&limit=2'));
  const rows = run(plan);
  assert.equal(plan.limit, 2);
  assert.equal(rows.length, 3);
  assert.equal(rows[0].lga, 'Brisbane');
});

test('aggregates group, count and sum', () => {
  const plan = aggregateQuery(
    'v_t_20260424',
    fields,
    P('group=year&metric=count,sum.fatal&order=year.desc'),
  );
  assert.deepEqual(run(plan), [
    { year: 2021, count: 2, sum_fatal: 1 },
    { year: 2020, count: 2, sum_fatal: 1 },
  ]);
  assert.deepEqual(
    run(aggregateQuery('v_t_20260424', fields, P('metric=count&lga=eq.Gold Coast'))),
    [{ count: 2 }],
  );
  assert.throws(() => aggregateQuery('t', fields, P('metric=sum.lga')), /numeric field/);
  assert.throws(() => aggregateQuery('t', fields, P('metric=median.year')), /metric is one of/);
});

test('ties keep group order, and rows without an order follow the file a table was loaded from', () => {
  const plan = aggregateQuery(
    'v_t_20260424',
    fields,
    P('group=year&metric=count&order=count.desc'),
  );
  assert.match(plan.sql, / ORDER BY "count" DESC, "year" LIMIT/);
  assert.match(rowsQuery('t', fields, P('')).sql, / ORDER BY rowid LIMIT/);
  assert.match(
    rowsQuery('t', fields, P('order=lga.desc')).sql,
    / ORDER BY "lga" DESC, rowid LIMIT/,
  );
  assert.match(
    rowsQuery('t', fields, P(''), ['year', 'lga', 'not_a_field']).sql,
    / ORDER BY "year" ASC NULLS LAST, "lga" ASC NULLS LAST, rowid LIMIT/,
  );
});

test('csv quotes only what needs it', () => {
  assert.equal(
    toCSV(
      ['a', 'b'],
      [
        { a: 'x,y', b: null },
        { a: 'say "hi"', b: 2 },
      ],
    ),
    'a,b\n"x,y",\n"say ""hi""",2\n',
  );
});

test('like and ilike ignore case in ASCII letters only, as SQLite does', () => {
  db.prepare('INSERT INTO "v_t_20260424" VALUES (?, ?, ?, ?)').run('Éden Park', 2022, 0, 50);
  const n = (qs) => run(rowsQuery('v_t_20260424', fields, P(qs))).length;
  for (const op of ['like', 'ilike']) {
    assert.equal(n(`lga=${op}.gold*`), 2, op);
    assert.equal(n(`lga=${op}.GOLD COAST`), 2, op);
    assert.equal(n(`lga=${op}.ÉDEN*`), 1, op);
    assert.equal(n(`lga=${op}.éden*`), 0, op);
  }
  db.exec('DELETE FROM "v_t_20260424" WHERE lga = \'Éden Park\'');
});
