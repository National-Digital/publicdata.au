import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { beforeEach, test } from 'node:test';
import { locate, recordId, searchPlan, terms } from './_catalogue.js';
import { onRequestGet } from './api/v1/catalogue.js';
import { onRequestPost as request } from './api/v1/requests.js';

const cache = new Map();
globalThis.caches = {
  default: {
    match: async (r) => cache.get(r.url)?.clone(),
    put: async (r, res) => {
      cache.set(r.url, res);
    },
  },
};

beforeEach(() => cache.clear());

const CASES = JSON.parse(
  readFileSync(new URL('../pipeline/tests/fixtures/locate.json', import.meta.url)),
);

test('a pasted URL is read the way directory.locate reads it', () => {
  for (const c of CASES) {
    const hit = locate(c.url);
    assert.deepEqual(hit && [hit.host, hit.kind, hit.value], c.expect, c.url);
  }
});

test('typed words become quoted terms, so no FTS syntax gets through', () => {
  assert.equal(terms('road crash'), '"road" "crash"');
  assert.equal(terms('"a" OR b* NEAR(c)'), '"a" "or" "b" "near" "c"');
  assert.equal(terms('  '), '');
  assert.equal(recordId('act', '3U5A-ve4j'), 'act-3u5a-ve4j');
});

test('limits and filters are checked before any SQL is written', () => {
  assert.throws(() => searchPlan('t', new URLSearchParams('limit=500')), /limit/);
  assert.throws(() => searchPlan('t', new URLSearchParams('jur=nz')), /jur/);
  assert.throws(() => searchPlan('t', new URLSearchParams('state=open')), /state/);
  const p = searchPlan('t', new URLSearchParams('q=bus&jur=act&state=votable,chosen'));
  assert.deepEqual(p.binds, ['"bus"', 'act', 'votable', 'chosen']);
});

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

// The table and index d1.catalogue_loads creates, with four records.
const T = 'v__catalogue_20260928';
const db = new DatabaseSync(':memory:');
const F = [
  'id',
  'title',
  'summary',
  'publisher',
  'publisher_path',
  'jur',
  'portal',
  'host',
  'name',
  'url',
  'licence',
  'formats',
  'modified',
  'state',
  'vote',
  'note',
];
db.exec(`CREATE TABLE _versions (slug TEXT, version TEXT, tbl TEXT, fields TEXT, rows INTEGER, attribution TEXT, header TEXT);
CREATE TABLE "${T}" (${F.map((f) => f + ' TEXT').join(', ')});
CREATE VIRTUAL TABLE "${T}_fts" USING fts5(title, summary, publisher, content='${T}', content_rowid='rowid', tokenize='porter unicode61 remove_diacritics 2');`);
const ins = db.prepare(`INSERT INTO "${T}" VALUES (${F.map(() => '?').join(', ')})`);
const rec = (o) => ins.run(...F.map((f) => o[f] ?? ''));
rec({
  id: 'act-3u5a-ve4j',
  title: 'Bus stops',
  summary: 'Every bus stop in Canberra.',
  publisher: 'Transport Canberra',
  publisher_path: '/act/transport-canberra/',
  jur: 'act',
  portal: 'act',
  host: 'data.act.gov.au',
  name: '3u5a-ve4j',
  url: 'https://www.data.act.gov.au/d/3u5a-ve4j',
  licence: 'CC-BY-4.0',
  state: 'votable',
  vote: 'act-3u5a-ve4j',
  modified: '2026-01-01',
});
rec({
  id: 'gov-1111',
  title: 'ABN Bulk Extract',
  publisher: 'Australian Business Register',
  publisher_path: '/cth/abr/',
  jur: 'cth',
  portal: 'gov',
  host: 'data.gov.au',
  name: 'abn-bulk-extract',
  url: 'https://data.gov.au/data/dataset/abn-bulk-extract',
  licence: 'CC-BY-3.0-AU',
  state: 'chosen',
  vote: 'abn-bulk-extract',
});
rec({
  id: 'nsw-2222',
  title: 'Bus timetables',
  publisher: 'Transport for NSW',
  publisher_path: '/nsw/tfnsw/',
  jur: 'nsw',
  portal: 'nsw',
  host: 'data.nsw.gov.au',
  name: 'bus-timetables',
  url: 'https://data.nsw.gov.au/data/dataset/bus-timetables',
  licence: 'none stated',
  state: 'closed',
  note: 'no licence stated',
});
rec({
  id: 'qld-3333',
  title: 'Road crash locations',
  publisher: 'Transport and Main Roads',
  publisher_path: '/qld/tmr/',
  jur: 'qld',
  portal: 'qld',
  host: 'data.qld.gov.au',
  name: 'crash-data-from-queensland-roads',
  url: 'https://www.data.qld.gov.au/dataset/crash-data-from-queensland-roads',
  licence: 'CC-BY-4.0',
  state: 'served',
  note: '/c/qld-road-crashes/',
});
db.exec(
  `INSERT INTO "${T}_fts" (rowid, title, summary, publisher) SELECT rowid, title, summary, publisher FROM "${T}";`,
);
db.prepare('INSERT INTO _versions VALUES (?, ?, ?, ?, ?, ?, ?)').run(
  '_catalogue',
  '2026-09-28',
  T,
  '[]',
  4,
  '',
  '{}',
);

const votes = new Map();
const env = {
  DB: d1(db),
  VOTES: {
    get: async (k) => (votes.has(k) ? { text: async () => votes.get(k) } : null),
    put: async (k, v) => {
      votes.set(k, v);
    },
    head: async (k) => (votes.has(k) ? {} : null),
    delete: async (k) => {
      votes.delete(k);
    },
    list: async ({ prefix }) => ({
      objects: [...votes.keys()].filter((k) => k.startsWith(prefix)).map((key) => ({ key })),
      truncated: false,
    }),
  },
  ASSETS: {
    fetch: async (u) => {
      const p = new URL(String(u.url || u)).pathname;
      if (p === '/backlog.json')
        return Response.json({ entries: [{ slug: 'abn-bulk-extract', status: 'backlog' }] });
      if (p === '/catalogue/votable/act-3.json') return Response.json({ 'act-3u5a-ve4j': [] });
      return new Response('', { status: 404 });
    },
  },
};
const get = async (qs) =>
  (
    await onRequestGet({
      request: new Request('https://publicdata.au/api/v1/catalogue?' + qs),
      env,
    })
  ).json();
const post = async (url) =>
  (
    await request({
      request: new Request('https://publicdata.au/api/v1/requests', {
        method: 'POST',
        body: JSON.stringify({ url }),
      }),
      env,
    })
  ).json();

test('search ranks what can take a vote first and greys nothing out of the answer', async () => {
  const b = await get('q=bus');
  assert.equal(b.total, 2);
  assert.deepEqual(
    b.rows.map((r) => r.state),
    ['votable', 'closed'],
  );
  assert.equal(b.rows[1].reason, 'no licence stated');
  assert.equal((await get('q=bus&state=votable,chosen')).total, 1);
  assert.equal((await get('q=bus&offset=1')).total, undefined);
  assert.equal((await get('jur=qld')).rows[0].page, 'https://publicdata.au/c/qld-road-crashes/');
  assert.deepEqual(
    (await get('ids=abn-bulk-extract')).rows.map((r) => r.id),
    ['gov-1111'],
  );
});

test('a pasted link votes for the record it names, and an unknown link is not stored', async () => {
  const v = await post('https://www.data.act.gov.au/Transport/Bus-Stops/3u5a-ve4j');
  assert.equal(v.status, 'voted');
  assert.equal(v.votes, 1);
  const c = await post('https://data.gov.au/data/dataset/abn-bulk-extract/');
  assert.equal(c.status, 'voted');
  assert.equal(c.record.vote, 'abn-bulk-extract');
  assert.equal(
    (await post('https://data.nsw.gov.au/data/dataset/bus-timetables')).status,
    'closed',
  );
  assert.equal(
    (await post('https://www.data.qld.gov.au/dataset/crash-data-from-queensland-roads/resource/x'))
      .status,
    'served',
  );
  const before = votes.size;
  const n = await post('https://www.example.gov.au/reports/');
  assert.equal(n.status, 'not_found');
  assert.match(n.contact, /nationaldigital\.com\.au\/contact/);
  assert.equal(votes.size, before);
});

test('without the index loaded, search and links say so', async () => {
  const r = await onRequestGet({
    request: new Request('https://publicdata.au/api/v1/catalogue?q=x'),
    env: {},
  });
  assert.equal(r.status, 503);
});

test('search and links share the fair-use limit per address', async () => {
  cache.clear();
  const ask = () =>
    onRequestGet({
      request: new Request('https://publicdata.au/api/v1/catalogue?q=bus', {
        headers: { 'cf-connecting-ip': '203.0.113.9' },
      }),
      env,
    });
  let last;
  for (let i = 0; i < 21; i++) last = await ask();
  assert.equal(last.status, 429);
  assert.ok(Number(last.headers.get('retry-after')) > 0);
  const other = await onRequestGet({
    request: new Request('https://publicdata.au/api/v1/catalogue?q=bus', {
      headers: { 'cf-connecting-ip': '203.0.113.10' },
    }),
    env,
  });
  assert.equal(other.status, 200);
  cache.clear();
});

test('search_datasets reads the served index, and catalog.json without it', async () => {
  const { searchServed, scanCatalog } = await import('./_served.js');
  const S = 'v__served_abc';
  const s = new DatabaseSync(':memory:');
  const G = [
    'slug',
    'title',
    'summary',
    'publisher',
    'jur',
    'licence',
    'page',
    'latest',
    'keywords',
    'fields',
  ];
  s.exec(`CREATE TABLE _versions (slug TEXT, version TEXT, tbl TEXT, fields TEXT, rows INTEGER, attribution TEXT, header TEXT);
INSERT INTO _versions VALUES ('_served', 'abc', '${S}', '[]', 2, '', '{}');
CREATE TABLE "${S}" (${G.map((f) => f + ' TEXT').join(', ')});
CREATE VIRTUAL TABLE "${S}_fts" USING fts5(title, summary, publisher, keywords, fields, content='${S}', content_rowid='rowid', tokenize='porter unicode61 remove_diacritics 2');`);
  const put = s.prepare(`INSERT INTO "${S}" VALUES (${G.map(() => '?').join(', ')})`);
  put.run(
    'qld-road-casualties',
    'Road casualties, Queensland',
    'Casualties by year',
    'Transport and Main Roads',
    'qld',
    'https://creativecommons.org/licenses/by/4.0/',
    'https://publicdata.au/d/qld-road-casualties/',
    'https://publicdata.au/d/qld-road-casualties/latest/data.parquet',
    'road toll',
    'casualty_count Casualties',
  );
  put.run(
    'au-road-deaths',
    'Road deaths, Australia',
    'Every death since 1989',
    'BITRE',
    'cth',
    'x',
    'p',
    'l',
    'fatalities',
    'speed_limit Speed limit',
  );
  s.exec(
    `INSERT INTO "${S}_fts" (rowid, title, summary, publisher, keywords, fields) SELECT rowid, title, summary, publisher, keywords, fields FROM "${S}"`,
  );
  const env = { DB: d1(s) };
  assert.deepEqual(
    (await searchServed(env, 'casualties')).map((r) => r.slug),
    ['qld-road-casualties'],
  );
  assert.deepEqual(
    (await searchServed(env, 'speed limit')).map((r) => r.slug),
    ['au-road-deaths'],
  );
  assert.deepEqual(Object.keys((await searchServed(env, 'road'))[0]), [
    'slug',
    'title',
    'publisher',
    'licence',
    'page',
    'latest',
  ]);
  assert.equal((await searchServed(env, '')).length, 2);
  assert.equal(await searchServed({}, 'road'), null);
  assert.equal(await searchServed({ DB: d1(new DatabaseSync(':memory:')) }, 'road'), null);
  const cat = {
    dataset: [
      {
        identifier: 'x',
        title: 'Crashes',
        license: 'l',
        landingPage: 'p',
        distribution: [{ accessURL: 'a' }],
      },
    ],
  };
  assert.deepEqual(scanCatalog(cat, 'crash'), [
    { slug: 'x', title: 'Crashes', publisher: undefined, licence: 'l', page: 'p', latest: 'a' },
  ]);
});
