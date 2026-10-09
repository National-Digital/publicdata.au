import { SLUG, aliases, count, json, others, today, voter } from '../../../_lib.js';

// A register entry still waiting to be built, or a catalogue record with an open licence and a
// download that is not served yet. Catalogue ids are checked against one small shard file.
async function allowed(env, slug) {
  const res = await env.ASSETS.fetch(new URL('/backlog.json', 'https://publicdata.au'));
  if (res.ok) {
    const { entries } = await res.json();
    const e = entries.find((x) => x.slug === slug);
    if (e) {
      return e.status !== 'live' && e.status !== 'building';
    }
  }
  const m = slug.match(/^([a-z]+)-([a-z0-9])/);
  if (!m) {
    return false;
  }
  const shard = await env.ASSETS.fetch(
    new URL(`/catalogue/votable/${m[1]}-${m[2]}.json`, 'https://publicdata.au'),
  );
  if (!shard.ok) {
    return false;
  }
  return Object.prototype.hasOwnProperty.call(await shard.json(), slug);
}

// A vote for a record id a register entry has claimed is the entry's vote.
async function target(env, raw) {
  const al = await aliases(env);
  return { al, slug: al[raw] || raw };
}

const keyFor = async (request, env, slug) =>
  `v/${slug}/${today()}/${await voter(request, env, slug)}`;

export async function onRequestPost({ request, env, params }) {
  if (!SLUG.test(params.slug)) {
    return json({ error: 'Unknown dataset' }, 400);
  }
  const { al, slug } = await target(env, params.slug);
  if (!(await allowed(env, slug))) {
    return json({ error: 'That dataset is not open for votes' }, 404);
  }
  // One vote a day per browser, including one cast earlier today under the record id.
  const keys = await Promise.all([slug, ...others(al, slug)].map((s) => keyFor(request, env, s)));
  const cast = await Promise.all(keys.map((k) => env.VOTES.head(k)));
  if (!cast.some(Boolean)) {
    await env.VOTES.put(keys[0], '1');
  }
  return json({ slug, votes: await count(env, slug, al) });
}

export async function onRequestDelete({ request, env, params }) {
  if (!SLUG.test(params.slug)) {
    return json({ error: 'Unknown dataset' }, 400);
  }
  const { al, slug } = await target(env, params.slug);
  const keys = await Promise.all([slug, ...others(al, slug)].map((s) => keyFor(request, env, s)));
  await Promise.all(keys.map((k) => env.VOTES.delete(k)));
  return json({ slug, votes: await count(env, slug, al) });
}

export async function onRequestGet({ env, params }) {
  if (!SLUG.test(params.slug)) {
    return json({ error: 'Unknown dataset' }, 400);
  }
  const { al, slug } = await target(env, params.slug);
  return json({ slug, votes: await count(env, slug, al) }, 200, {
    'cache-control': 'public, max-age=30',
  });
}
