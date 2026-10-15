export const json = (body, status = 200, extra = {}) =>
  new Response(JSON.stringify(body), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store',
      'access-control-allow-origin': '*',
      ...extra,
    },
  });

export const SLUG = /^[a-z0-9][a-z0-9-]{1,63}$/;

// Content-Encoding is a list of tokens; an upload can leave aws-chunked beside gzip.
export const gzipped = (obj) =>
  (obj.httpMetadata?.contentEncoding || '')
    .split(',')
    .some((t) => t.trim().toLowerCase() === 'gzip');

export const gunzip = (body) => new Response(body.pipeThrough(new DecompressionStream('gzip')));

// An R2 binding returns the bytes as stored, and a text file over GZIP_MIN is stored gzipped.
export const storedText = (obj) => (gzipped(obj) ? gunzip(obj.body).text() : obj.text());

export const today = () => new Date().toISOString().slice(0, 10);

// The hash is keyed by the Pages secret VOTE_SALT, held outside the votes bucket, so reading the
// bucket alone cannot test an address. Without the secret nothing is hashed at all.
export const SALT_MIN = 32;
export const salted = (env) =>
  typeof env.VOTE_SALT === 'string' && env.VOTE_SALT.length >= SALT_MIN;

// One vote per browser per day: a keyed hash of address and user agent that is never exposed.
export async function voter(request, env, slug) {
  if (!salted(env)) {
    throw new Error('VOTE_SALT is not set');
  }
  const ip = request.headers.get('cf-connecting-ip') || '0.0.0.0';
  const ua = request.headers.get('user-agent') || '';
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    'raw',
    enc.encode(env.VOTE_SALT),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  const mac = await crypto.subtle.sign('HMAC', key, enc.encode(`${today()}|${slug}|${ip}|${ua}`));
  return [...new Uint8Array(mac)]
    .slice(0, 12)
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

async function listed(env, prefix) {
  let n = 0,
    cursor;
  do {
    const page = await env.VOTES.list({ prefix, cursor, limit: 1000 });
    n += page.objects.length;
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
  return n;
}

// Catalogue record ids a register entry has since claimed, mapped to the entry's slug. Votes cast
// under the id before the entry existed still count for it. The file changes only with a deploy.
let known;
export async function aliases(env) {
  if (known) {
    return known;
  }
  try {
    const r = await env.ASSETS.fetch(new URL('/catalogue/aliases.json', 'https://publicdata.au'));
    if (!r.ok) {
      return {};
    }
    known = await r.json();
  } catch {
    return {};
  }
  return known;
}

export const others = (al, slug) => Object.keys(al).filter((id) => al[id] === slug);

// The small file a catalogue id is looked up in, named by portal and first character.
export const shardOf = (id) => {
  const m = id.match(/^([a-z]+)-([a-z0-9])/);
  return m ? `${m[1]}-${m[2]}` : null;
};

// A shard of the copies list that could not be read. It is not the same as "not a copy", which
// would count a copy's votes apart and let the contributor sync close its issue.
export class CopiesUnreadable extends Error {}

// One shard of a catalogue list keyed by record id. Each is read once, since the files change
// only with a deploy; an absent one holds none.
// The read itself is kept, so lookups that start together share one fetch.
const shards = new Map();
async function readShard(env, name) {
  let r;
  try {
    r = await env.ASSETS.fetch(new URL(`/${name}`, 'https://publicdata.au'));
    if (r.ok) {
      return await r.json();
    }
  } catch {
    throw new CopiesUnreadable(`${name} could not be read`);
  }
  if (r.status === 404) {
    return {};
  }
  throw new CopiesUnreadable(`${name} answered ${r.status}`);
}
function shardFile(env, dir, key) {
  const name = `catalogue/${dir}/${key}.json`;
  if (!shards.has(name)) {
    const read = readShard(env, name);
    shards.set(name, read);
    read.catch(() => shards.delete(name));
  }
  return shards.get(name);
}

const own = (o, id) => Object.prototype.hasOwnProperty.call(o, id);

// A record id the catalogue dropped as a copy, mapped to the record that replaced it, or null.
export async function replacedBy(env, id) {
  const key = shardOf(id);
  if (!key) {
    return null;
  }
  const got = await shardFile(env, 'copies', key);
  return own(got, id) ? got[id] : null;
}

// The copies a record that takes no votes replaced. A votable record lists them in its entry.
export async function copiesOf(env, id) {
  const key = shardOf(id);
  if (!key) {
    return [];
  }
  const got = await shardFile(env, 'copied', key);
  return own(got, id) ? got[id] : [];
}

// The key a vote under this id counts for: a claimed record's register slug, or the record a
// dropped copy was replaced by.
export async function follow(env, al, raw) {
  if (al[raw]) {
    return al[raw];
  }
  const to = await replacedBy(env, raw);
  return to ? al[to] || to : raw;
}

export async function count(env, slugs) {
  const n = await Promise.all(slugs.map((s) => listed(env, `v/${s}/`)));
  return n.reduce((a, b) => a + b, 0);
}

// Every slug with votes, with aliases and dropped copies folded into the key they count for.
export async function tally(env, al = {}) {
  const raw = {};
  let cursor;
  do {
    const page = await env.VOTES.list({ prefix: 'v/', cursor, limit: 1000 });
    for (const o of page.objects) {
      const k = o.key.split('/')[1];
      raw[k] = (raw[k] || 0) + 1;
    }
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
  const keys = Object.keys(raw);
  const to = await Promise.all(keys.map((k) => follow(env, al, k)));
  const counts = {};
  keys.forEach((k, i) => {
    counts[to[i]] = (counts[to[i]] || 0) + raw[k];
  });
  return counts;
}
