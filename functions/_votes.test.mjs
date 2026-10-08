import assert from 'node:assert/strict';
import { test } from 'node:test';
import { FRESH_MS, ROLLUP, onRequestGet as all } from './api/v1/votes.js';
import {
  onRequestDelete as unvote,
  onRequestGet as one,
  onRequestPost as vote,
} from './api/v1/votes/[slug].js';

const store = new Map();
let listings = 0;
const env = {
  VOTES: {
    get: async (k) => (store.has(k) ? { text: async () => store.get(k) } : null),
    put: async (k, v) => {
      store.set(k, v);
    },
    head: async (k) => (store.has(k) ? {} : null),
    delete: async (k) => {
      store.delete(k);
    },
    list: async ({ prefix }) => {
      listings++;
      return {
        objects: [...store.keys()].filter((k) => k.startsWith(prefix)).map((key) => ({ key })),
        truncated: false,
      };
    },
  },
  ASSETS: {
    fetch: async (u) => {
      const p = new URL(String(u.url || u)).pathname;
      if (p === '/backlog.json')
        return Response.json({
          entries: [
            { slug: 'abn-bulk-extract', status: 'backlog' },
            { slug: 'crashes', status: 'live' },
          ],
        });
      if (p === '/catalogue/aliases.json') return Response.json({ 'gov-1111': 'abn-bulk-extract' });
      if (p === '/catalogue/votable/act-3.json') return Response.json({ 'act-3u5a-ve4j': [] });
      return new Response('', { status: 404 });
    },
  },
};
const req = (ip) =>
  new Request('https://publicdata.au/api/v1/votes/x', {
    method: 'POST',
    headers: { 'cf-connecting-ip': ip },
  });
const call = async (fn, slug, ip = '203.0.113.1') =>
  (await fn({ request: req(ip), env, params: { slug } })).json();

test('a vote on a claimed record id counts for the register entry, once a day per browser', async () => {
  // A vote cast under the record id before the register claimed it.
  store.set('v/gov-1111/2026-01-01/old', '1');
  const a = await call(vote, 'gov-1111');
  assert.deepEqual(a, { slug: 'abn-bulk-extract', votes: 2 });
  assert.ok([...store.keys()].some((k) => k.startsWith('v/abn-bulk-extract/')));
  assert.equal((await call(vote, 'abn-bulk-extract')).votes, 2);
  assert.equal((await call(vote, 'abn-bulk-extract', '203.0.113.2')).votes, 3);
  assert.equal((await call(one, 'gov-1111')).votes, 3);
  assert.equal((await call(unvote, 'gov-1111', '203.0.113.2')).votes, 2);
  assert.equal((await call(vote, 'crashes')).error, 'That dataset is not open for votes');
  assert.equal((await call(vote, 'act-3u5a-ve4j')).votes, 1);
});

test('the list of counts is one object, folded by alias and recounted once stale', async () => {
  store.delete(ROLLUP);
  listings = 0;
  const read = async (waitUntil) => (await all({ env, waitUntil })).json();
  assert.deepEqual(await read(), { 'abn-bulk-extract': 2, 'act-3u5a-ve4j': 1 });
  assert.equal(listings, 1);
  store.set('v/act-3u5a-ve4j/2026-01-02/new', '1');
  assert.equal((await read())['act-3u5a-ve4j'], 1);
  assert.equal(listings, 1);
  const old = JSON.parse(store.get(ROLLUP));
  store.set(ROLLUP, JSON.stringify({ ...old, at: Date.now() - FRESH_MS - 1 }));
  const later = [];
  assert.equal((await read((p) => later.push(p)))['act-3u5a-ve4j'], 1);
  await Promise.all(later);
  assert.equal((await read())['act-3u5a-ve4j'], 2);
});
