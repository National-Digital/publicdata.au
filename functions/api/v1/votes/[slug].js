import {
  SLUG,
  CopiesUnreadable,
  aliases,
  copiesOf,
  count,
  follow,
  json,
  others,
  salted,
  shardOf,
  today,
  voter,
} from '../../../_lib.js';

// A votable catalogue record's entry in its shard file, or null.
async function votable(env, id) {
  const key = shardOf(id);
  if (!key) {
    return null;
  }
  const shard = await env.ASSETS.fetch(
    new URL(`/catalogue/votable/${key}.json`, 'https://publicdata.au'),
  );
  if (shard.status === 404) {
    return null;
  }
  // A shard that cannot be read is not "not votable", which would count the copies from
  // copied/ and leave out the copies a votable record carries.
  if (!shard.ok) {
    throw new CopiesUnreadable(`catalogue/votable/${key}.json answered ${shard.status}`);
  }
  const ids = await shard.json();
  return Object.prototype.hasOwnProperty.call(ids, id) ? ids[id] : null;
}

// A register entry still waiting to be built, or a catalogue record with an open licence and a
// download that is not served yet. Catalogue ids are checked against one small shard file.
async function allowed(env, slug, entry) {
  const res = await env.ASSETS.fetch(new URL('/backlog.json', 'https://publicdata.au'));
  if (res.ok) {
    const { entries } = await res.json();
    const e = entries.find((x) => x.slug === slug);
    if (e) {
      return e.status !== 'live' && e.status !== 'building';
    }
  }
  return entry !== null;
}

// A vote for a record id a register entry has claimed is the entry's vote, and a vote for a
// dropped copy is the vote of the record that replaced it. Votes stored under either count, for
// a record that takes no votes too.
async function target(env, raw) {
  const al = await aliases(env);
  const slug = await follow(env, al, raw);
  const entry = await votable(env, slug);
  const copies = entry ? entry[4] || [] : await copiesOf(env, slug);
  return { slug, entry, under: [slug, ...others(al, slug), ...copies] };
}

const keyFor = async (request, env, slug) =>
  `v/${slug}/${today()}/${await voter(request, env, slug)}`;

const unkeyed = () =>
  json({ error: 'Votes cannot be counted at the moment. Try again later.' }, 503);

// An unreadable copies shard pauses votes; it never counts a copy's votes apart.
const guarded = (fn) => async (ctx) => {
  try {
    return await fn(ctx);
  } catch (e) {
    if (e instanceof CopiesUnreadable) {
      return unkeyed();
    }
    throw e;
  }
};

export const onRequestPost = guarded(async ({ request, env, params }) => {
  if (!SLUG.test(params.slug)) {
    return json({ error: 'Unknown dataset' }, 400);
  }
  if (!salted(env)) {
    return unkeyed();
  }
  const { slug, entry, under } = await target(env, params.slug);
  if (!(await allowed(env, slug, entry))) {
    return json({ error: 'That dataset is not open for votes' }, 404);
  }
  // One vote a day per browser, including one cast earlier today under the record id.
  const keys = await Promise.all(under.map((s) => keyFor(request, env, s)));
  const cast = await Promise.all(keys.map((k) => env.VOTES.head(k)));
  if (!cast.some(Boolean)) {
    await env.VOTES.put(keys[0], '1');
  }
  return json({ slug, votes: await count(env, under) });
});

export const onRequestDelete = guarded(async ({ request, env, params }) => {
  if (!SLUG.test(params.slug)) {
    return json({ error: 'Unknown dataset' }, 400);
  }
  if (!salted(env)) {
    return unkeyed();
  }
  const { slug, under } = await target(env, params.slug);
  const keys = await Promise.all(under.map((s) => keyFor(request, env, s)));
  await Promise.all(keys.map((k) => env.VOTES.delete(k)));
  return json({ slug, votes: await count(env, under) });
});

export const onRequestGet = guarded(async ({ env, params }) => {
  if (!SLUG.test(params.slug)) {
    return json({ error: 'Unknown dataset' }, 400);
  }
  const { slug, under } = await target(env, params.slug);
  return json({ slug, votes: await count(env, under) }, 200, {
    'cache-control': 'public, max-age=30',
  });
});
