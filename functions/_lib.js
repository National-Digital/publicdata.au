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

// The salt lives only in the private votes bucket and is created on first use, so a missing
// secret cannot degrade the hash to something precomputable. No bucket, no vote.
async function salt(env) {
  if (!env.VOTES) {
    throw new Error('VOTES bucket is not bound');
  }
  const o = await env.VOTES.get('_salt');
  if (o) {
    return o.text();
  }
  const s = crypto.randomUUID() + crypto.randomUUID();
  // Create only if still absent, then read back so two first requests settle on one value.
  try {
    await env.VOTES.put('_salt', s, { onlyIf: { etagDoesNotMatch: '*' } });
  } catch {
    // Another request created it first; the read below takes that value.
  }
  const stored = await env.VOTES.get('_salt');
  return stored ? stored.text() : s;
}

// One vote per browser per day: a salted hash of address and user agent that is never exposed.
export async function voter(request, env, slug) {
  const ip = request.headers.get('cf-connecting-ip') || '0.0.0.0';
  const ua = request.headers.get('user-agent') || '';
  const data = new TextEncoder().encode(`${await salt(env)}|${today()}|${slug}|${ip}|${ua}`);
  const digest = await crypto.subtle.digest('SHA-256', data);
  return [...new Uint8Array(digest)]
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
