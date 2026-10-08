import { aliases, json, tally } from '../../_lib.js';

// The counts are kept in one object and recounted from the vote objects once it is a minute old,
// so a read is one object and any drift is gone within the minute.
export const ROLLUP = '_counts.json';
export const FRESH_MS = 60_000;

async function recount(env) {
  const body = { at: Date.now(), counts: await tally(env, await aliases(env)) };
  await env.VOTES.put(ROLLUP, JSON.stringify(body));
  return body;
}

// Every slug with at least one vote. Slugs with none are simply absent.
export async function onRequestGet({ env, waitUntil }) {
  const o = await env.VOTES.get(ROLLUP);
  let body = o ? JSON.parse(await o.text()) : null;
  if (!body) {
    body = await recount(env);
  } else if (Date.now() - body.at > FRESH_MS) {
    const next = recount(env);
    if (waitUntil) {
      waitUntil(next);
    } else {
      body = await next;
    }
  }
  return json(body.counts, 200, { 'cache-control': 'public, max-age=30' });
}
