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

export async function count(env, slug, al = {}) {
  const n = await Promise.all([slug, ...others(al, slug)].map((s) => listed(env, `v/${s}/`)));
  return n.reduce((a, b) => a + b, 0);
}

// Every slug with votes, with aliases folded into their register slug.
export async function tally(env, al = {}) {
  const counts = {};
  let cursor;
  do {
    const page = await env.VOTES.list({ prefix: 'v/', cursor, limit: 1000 });
    for (const o of page.objects) {
      const raw = o.key.split('/')[1];
      const slug = al[raw] || raw;
      counts[slug] = (counts[slug] || 0) + 1;
    }
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
  return counts;
}
