// Search and URL lookup over the catalogue index the deploy loads into D1 (pipeline/publicdata/d1.py).

export const CATALOGUE = '_catalogue';
export const LIMIT_MAX = 50;
export const JURS = ['cth', 'nsw', 'vic', 'qld', 'wa', 'sa', 'tas', 'act', 'nt'];
const STATES = ['votable', 'chosen', 'served', 'closed'];
const COLS =
  'id, title, summary, publisher, publisher_path, jur, host, url, licence, formats, modified, state, vote, note';

export class CatalogueError extends Error {}

// record_id() in pipeline/publicdata/catalogue.py.
export const recordId = (portal, source) =>
  `${portal}-${source}`
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64);

export const bare = (u) =>
  (u.hostname || '').replace(/^www\./, '') + u.pathname.replace(/\/+$/, '');

// PORTAL_HOSTS in pipeline/publicdata/directory.py.
const PORTAL_HOSTS = {
  'dataexplorer.abs.gov.au': 'data.api.abs.gov.au',
  'explore.data.abs.gov.au': 'data.api.abs.gov.au',
};

// The same rules as locate() in pipeline/publicdata/directory.py; functions/_catalogue.test.mjs
// holds the two together.
export function locate(raw) {
  let u;
  try {
    u = new URL(String(raw || '').trim());
  } catch {
    return null;
  }
  let host = u.hostname.toLowerCase().replace(/^www\./, '');
  if (!host) return null;
  host = PORTAL_HOSTS[host] || host;
  let m = u.pathname.match(/\/datasets?\/([^/?#]+)/);
  if (m)
    return { host, kind: 'name', value: decodeURIComponent(m[1]).toLowerCase(), bare: bare(u) };
  m = u.pathname.match(/(?:^|\/)([a-z0-9]{4}-[a-z0-9]{4})(?:\/|$)/);
  if (m) return { host, kind: 'source', value: m[1], bare: bare(u) };
  const df = u.searchParams.get('df[id]');
  if (df) return { host, kind: 'source', value: df, bare: bare(u) };
  m = u.pathname.match(/\/data\/[^/,]+,([^/,]+),/);
  if (m) return { host, kind: 'source', value: m[1], bare: bare(u) };
  return { host, kind: 'url', value: bare(u), bare: bare(u) };
}

// Words become quoted FTS5 terms, so nothing typed is read as syntax. The porter stemmer matches
// plurals and other endings, and a prefix match would make "bus" find "business".
export function terms(q) {
  const words =
    String(q || '')
      .toLowerCase()
      .match(/[\p{L}\p{N}]+/gu) || [];
  if (!words.length) return '';
  return words
    .slice(0, 12)
    .map((w) => `"${w}"`)
    .join(' ');
}

export function searchPlan(tbl, params) {
  const binds = [];
  const where = [];
  const jur = params.get('jur');
  if (jur) {
    if (!JURS.includes(jur)) throw new CatalogueError(`jur is one of ${JURS.join(', ')}`);
    where.push('c.jur = ?');
    binds.push(jur);
  }
  const state = params.get('state');
  if (state) {
    const want = state.split(',');
    if (want.some((s) => !STATES.includes(s)))
      throw new CatalogueError(`state is one or more of ${STATES.join(', ')}`);
    where.push(`c.state IN (${want.map(() => '?').join(',')})`);
    binds.push(...want);
  }
  const limit = params.has('limit') ? Number(params.get('limit')) : 20;
  const offset = params.has('offset') ? Number(params.get('offset')) : 0;
  if (!Number.isInteger(limit) || limit < 1 || limit > LIMIT_MAX)
    throw new CatalogueError(`limit is 1 to ${LIMIT_MAX}`);
  if (!Number.isInteger(offset) || offset < 0 || offset > 5000)
    throw new CatalogueError('offset is 0 to 5000');
  const q = terms(params.get('q'));
  const from = q ? `"${tbl}_fts" f JOIN "${tbl}" c ON c.rowid = f.rowid` : `"${tbl}" c`;
  if (q) {
    where.unshift(`"${tbl}_fts" MATCH ?`);
    binds.unshift(q);
  }
  const w = where.length ? ' WHERE ' + where.join(' AND ') : '';
  // What can still take a vote comes before what is served or cannot be built.
  const rank = `CASE c.state WHEN 'votable' THEN 0 WHEN 'chosen' THEN 0 WHEN 'served' THEN 1 ELSE 2 END`;
  const order = q ? `${rank}, bm25("${tbl}_fts", 8.0, 1.0, 3.0)` : `${rank}, c.modified DESC, c.id`;
  return {
    sql: `SELECT ${COLS.split(', ')
      .map((c) => 'c.' + c)
      .join(', ')} FROM ${from}${w} ORDER BY ${order} LIMIT ${limit + 1} OFFSET ${offset}`,
    count: `SELECT COUNT(*) AS n FROM ${from}${w}`,
    binds,
    limit,
    offset,
  };
}

export async function table(env) {
  if (!env.DB) return null;
  try {
    return await env.DB.prepare(
      'SELECT tbl, version, rows FROM _versions WHERE slug = ? ORDER BY version DESC LIMIT 1',
    )
      .bind(CATALOGUE)
      .first();
  } catch {
    return null;
  }
}

// The record a pasted portal URL names, or null.
export async function resolve(env, tbl, raw) {
  const hit = locate(raw);
  if (!hit) return null;
  const one = (sql, ...b) =>
    env.DB.prepare(`SELECT ${COLS} FROM "${tbl}" WHERE ${sql} LIMIT 1`)
      .bind(...b)
      .first();
  let rec = null;
  if (hit.kind === 'name') rec = await one('host = ? AND name = ?', hit.host, hit.value);
  if (!rec && hit.kind !== 'url') {
    const p = await env.DB.prepare(`SELECT portal FROM "${tbl}" WHERE host = ? LIMIT 1`)
      .bind(hit.host)
      .first();
    if (p) rec = await one('id = ?', recordId(p.portal, hit.value));
  }
  return (
    rec ||
    (await one(
      'url IN (?, ?, ?, ?)',
      'https://' + hit.bare,
      'https://www.' + hit.bare,
      'http://' + hit.bare,
      'http://www.' + hit.bare,
    )) ||
    null
  );
}
