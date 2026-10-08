import assert from 'node:assert/strict';
import { test } from 'node:test';
import { canonical, problem, viewId } from './_views.js';

const good = {
  slug: 'test-data',
  version: '2026-04-24',
  workspace: {
    panels: { a: { plugin: 'Y Bar', group_by: ['lga'] } },
    layout: { type: 'tab-layout', tabs: ['a'] },
  },
};
const versions = ['2026-04-24'];

test('a dashboard over a known version is accepted', () => {
  assert.equal(problem(good, versions), null);
});

test('unknown datasets, versions and settings are refused', () => {
  assert.match(problem(good, null), /No such dataset/);
  assert.match(problem({ ...good, version: '2020-01-01' }, versions), /No such version/);
  assert.match(problem({ ...good, workspace: { panels: {} } }, versions), /1 to 12/);
  assert.match(
    problem({ ...good, workspace: { ...good.workspace, script: 1 } }, versions),
    /unknown key: script/,
  );
  assert.match(
    problem({ ...good, workspace: { panels: { a: { table: 'other' } } } }, versions),
    /unknown key: table/,
  );
  const many = Object.fromEntries([...Array(13).keys()].map((i) => [`p${i}`, {}]));
  assert.match(problem({ ...good, workspace: { panels: many } }, versions), /1 to 12/);
  let deep = {};
  for (let i = 0; i < 20; i++) {
    deep = { x: deep };
  }
  assert.match(
    problem({ ...good, workspace: { ...good.workspace, layout: deep } }, versions),
    /too deeply/,
  );
});

test('the id is a hash of the content, so a resave returns the same link', async () => {
  const a = await viewId(canonical(good));
  assert.match(a, /^[a-f0-9]{16}$/);
  assert.equal(a, await viewId(canonical({ ...good, extra: 'ignored' })));
  assert.notEqual(a, await viewId(canonical({ ...good, version: '2026-05-01' })));
});

function bucket() {
  const m = new Map();
  const obj = (k) => ({ key: k, body: new Response(m.get(k)).body, text: async () => m.get(k) });
  return {
    m,
    async get(k) {
      return m.has(k) ? obj(k) : null;
    },
    async head(k) {
      return m.has(k) ? { key: k } : null;
    },
    async put(k, v) {
      m.set(k, String(v));
    },
    async list({ prefix }) {
      return {
        objects: [...m.keys()].filter((k) => k.startsWith(prefix)).map((key) => ({ key })),
        truncated: false,
      };
    },
  };
}

test('POST stores a dashboard once and GET returns it', async () => {
  const { onRequestPost } = await import('./api/v1/views.js');
  const { onRequestGet } = await import('./api/v1/views/[id].js');
  const env = {
    VOTES: bucket(),
    ASSETS: {
      fetch: async (u) =>
        String(u).endsWith('/d/test-data/explore/versions.json')
          ? Response.json({ versions: ['2026-04-24'] })
          : new Response('', { status: 404 }),
    },
  };
  const post = (body) =>
    onRequestPost({
      request: new Request('https://publicdata.au/api/v1/views', {
        method: 'POST',
        body: JSON.stringify(body),
      }),
      env,
    });
  const r1 = await post(good);
  assert.equal(r1.status, 201);
  const { id, url } = await r1.json();
  assert.equal(url, `https://publicdata.au/d/test-data/explore/?view=${id}`);
  const r2 = await post(good);
  assert.equal((await r2.json()).id, id);
  assert.equal([...env.VOTES.m.keys()].filter((k) => k.startsWith('dash/')).length, 1);
  assert.equal((await post({ ...good, slug: 'nope' })).status, 400);
  const got = await onRequestGet({ env, params: { id } });
  assert.equal(got.status, 200);
  assert.match(got.headers.get('cache-control'), /immutable/);
  assert.deepEqual(await got.json(), JSON.parse(canonical(good)));
  assert.equal((await onRequestGet({ env, params: { id: '0000000000000000' } })).status, 404);
  assert.equal((await onRequestGet({ env, params: { id: '../x' } })).status, 404);
});
