import assert from 'node:assert/strict';
import { test } from 'node:test';
import { voter } from './_lib.js';
import { FRESH_MS, ROLLUP, onRequestGet as all } from './api/v1/votes.js';
import {
  onRequestDelete as unvote,
  onRequestGet as one,
  onRequestPost as vote,
} from './api/v1/votes/[slug].js';

// Built at runtime and plainly not a secret.
const FAKE_SALT = 'not-a-real-vote-salt-'.repeat(2);
const store = new Map();
let listings = 0;
const gets = [];
const env = {
  VOTE_SALT: FAKE_SALT,
  VOTES: {
    get: async (k) => {
      gets.push(k);
      return store.has(k) ? { text: async () => store.get(k) } : null;
    },
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
      if (p === '/backlog.json') {
        return Response.json({
          entries: [
            { slug: 'abn-bulk-extract', status: 'backlog' },
            { slug: 'crashes', status: 'live' },
          ],
        });
      }
      if (p === '/catalogue/aliases.json') {
        return Response.json({ 'gov-1111': 'abn-bulk-extract' });
      }
      if (p === '/catalogue/votable/act-3.json') {
        return Response.json({ 'act-3u5a-ve4j': [] });
      }
      return new Response('', { status: 404 });
    },
  },
};
const req = (ip, ua = '') =>
  new Request('https://publicdata.au/api/v1/votes/x', {
    method: 'POST',
    headers: { 'cf-connecting-ip': ip, 'user-agent': ua },
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

test('the hash is keyed by the secret, and the bucket holds and gives up no salt', async () => {
  await call(vote, 'abn-bulk-extract', '203.0.113.9');
  assert.deepEqual(
    gets.filter((k) => k !== ROLLUP),
    [],
  );
  assert.deepEqual(
    [...store.keys()].filter((k) => !k.startsWith('v/') && k !== ROLLUP),
    [],
  );
  const r = req('203.0.113.9', 'Mozilla/5.0');
  const a = await voter(r, env, 'crashes');
  assert.match(a, /^[0-9a-f]{24}$/);
  assert.equal(await voter(r, env, 'crashes'), a);
  assert.notEqual(await voter(r, { ...env, VOTE_SALT: FAKE_SALT + 'x' }, 'crashes'), a);
  assert.notEqual(await voter(r, env, 'abn-bulk-extract'), a);
});

test('without the salt secret votes are refused and counts still read', async () => {
  for (const salt of [undefined, '', FAKE_SALT.slice(0, 31)]) {
    const e = { ...env, VOTE_SALT: salt };
    const before = [...store.entries()];
    for (const fn of [vote, unvote]) {
      const r = await fn({
        request: req('203.0.113.7'),
        env: e,
        params: { slug: 'abn-bulk-extract' },
      });
      assert.equal(r.status, 503);
      assert.match((await r.json()).error, /cannot be counted/);
    }
    assert.deepEqual([...store.entries()], before);
    const one1 = await one({ request: req('203.0.113.7'), env: e, params: { slug: 'gov-1111' } });
    assert.equal(one1.status, 200);
    assert.equal((await one1.json()).slug, 'abn-bulk-extract');
    assert.equal((await all({ env: e })).status, 200);
    await assert.rejects(voter(req('203.0.113.7'), e, 'crashes'), /VOTE_SALT/);
  }
});
