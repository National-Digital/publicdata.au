import assert from 'node:assert/strict';
import { test } from 'node:test';
import { tally } from './_lib.js';
import { onRequestGet as all } from './api/v1/votes.js';
import {
  onRequestDelete as unvote,
  onRequestGet as one,
  onRequestPost as vote,
} from './api/v1/votes/[slug].js';

// Built at runtime and plainly not a secret.
const FAKE_SALT = 'not-a-real-vote-salt-'.repeat(2);
const store = new Map();
const files = {
  '/backlog.json': { entries: [{ slug: 'abn-bulk-extract', status: 'backlog' }] },
  '/catalogue/aliases.json': { 'gov-1111': 'abn-bulk-extract', 'vic-9999': 'abn-bulk-extract' },
  // nsw-a replaced data.gov.au's copy gov-a; gov-z was dropped with nothing to replace it.
  '/catalogue/copies/gov-a.json': { 'gov-aaaa': 'nsw-aaaa' },
  '/catalogue/copies/vic-9.json': { 'vic-9999': 'gov-1111' },
  '/catalogue/copies/gov-b.json': 500,
  // qld-cccc takes no votes, so its copies are listed in the copied shards.
  '/catalogue/copies/gov-c.json': { 'gov-cccc': 'qld-cccc' },
  '/catalogue/copied/qld-c.json': { 'qld-cccc': ['gov-cccc'] },
  '/catalogue/votable/nsw-a.json': {
    'nsw-aaaa': ['Title', '/nsw/x/', 'https://x', 'X', ['gov-aaaa']],
  },
};
const env = {
  VOTE_SALT: FAKE_SALT,
  VOTES: {
    get: async (k) => (store.has(k) ? { text: async () => store.get(k) } : null),
    put: async (k, v) => {
      store.set(k, v);
    },
    head: async (k) => (store.has(k) ? {} : null),
    delete: async (k) => {
      store.delete(k);
    },
    list: async ({ prefix }) => ({
      objects: [...store.keys()].filter((k) => k.startsWith(prefix)).map((key) => ({ key })),
      truncated: false,
    }),
  },
  ASSETS: {
    fetch: async (u) => {
      const p = new URL(String(u.url || u)).pathname;
      if (typeof files[p] === 'number') {
        return new Response('', { status: files[p] });
      }
      return p in files ? Response.json(files[p]) : new Response('', { status: 404 });
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

test('votes under a dropped copy count for the record that replaced it', async () => {
  // Cast while data.gov.au's copy was still in the catalogue.
  store.set('v/gov-aaaa/2026-01-01/old', '1');
  assert.deepEqual(await call(one, 'nsw-aaaa'), { slug: 'nsw-aaaa', votes: 1 });
  assert.deepEqual(await call(one, 'gov-aaaa'), { slug: 'nsw-aaaa', votes: 1 });
  // A new vote under the old id is stored under the record, once a day per browser.
  assert.deepEqual(await call(vote, 'gov-aaaa'), { slug: 'nsw-aaaa', votes: 2 });
  assert.ok(store.has([...store.keys()].find((k) => k.startsWith('v/nsw-aaaa/'))));
  assert.equal((await call(vote, 'nsw-aaaa')).votes, 2);
  assert.equal((await call(unvote, 'gov-aaaa')).votes, 1);
  assert.equal((await call(vote, 'nsw-aaaa', '203.0.113.2')).votes, 2);
  // A copy of a record the register claimed counts for the entry.
  store.set('v/vic-9999/2026-01-01/old', '1');
  assert.deepEqual(await call(one, 'vic-9999'), { slug: 'abn-bulk-extract', votes: 1 });
  // An id with no mapping stays as it is and cannot take a vote.
  assert.equal((await call(vote, 'gov-zzzz')).error, 'That dataset is not open for votes');
  store.set('v/gov-zzzz/2026-01-01/old', '1');
  const counts = await (await all({ env })).json();
  assert.deepEqual(counts, { 'nsw-aaaa': 2, 'abn-bulk-extract': 1, 'gov-zzzz': 1 });
});

test('a copies list that cannot be read pauses the vote and stores nothing', async () => {
  const before = [...store.keys()];
  for (const fn of [vote, unvote, one]) {
    const r = await fn({ request: req('203.0.113.5'), env, params: { slug: 'gov-bbbb' } });
    assert.equal(r.status, 503);
  }
  assert.deepEqual([...store.keys()], before);
});

test('a record that takes no votes counts the votes under its copies as the tally does', async () => {
  store.set('v/gov-cccc/2026-01-01/old', '1');
  assert.deepEqual(await call(one, 'qld-cccc'), { slug: 'qld-cccc', votes: 1 });
  assert.deepEqual(await call(one, 'gov-cccc'), { slug: 'qld-cccc', votes: 1 });
  assert.equal((await call(vote, 'qld-cccc')).error, 'That dataset is not open for votes');
  assert.equal((await tally(env))['qld-cccc'], 1);
});
