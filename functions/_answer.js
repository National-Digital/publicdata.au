// One query, whichever engine answers it. The query API and the MCP row tools both ask here, so
// the same query through either door gets the same answer. A version's rollup answers first where
// it holds the answer exactly, then D1 for the versions it holds, then the version's Parquet in R2
// for every other version. D1 answers exactly as it always has; the Parquet engine answers in the
// order of the file it reads, and says so.

import { SLUG } from './_lib.js';
import { QueryError, RESERVED, aggregateQuery, rowsQuery } from './_query.js';
import {
  BudgetError,
  ENGINE,
  FileError,
  StaleError,
  forget,
  openVersion,
  parquetAggregate,
  parquetRows,
} from './_parquet.js';
import { rollup } from './_rollup.js';

const SITE = 'https://publicdata.au';
const API = `${SITE}/api/v1/datasets`;
const VERSION = /^\d{4}-\d{2}-\d{2}$/;
const NOT_ENABLED =
  'The query API is not enabled yet. Every dataset is available as files under /d/.';
export const WITHHELD =
  'This dataset is withheld while its licence is reviewed. See https://publicdata.au/backlog/';

const enc = (v) => encodeURIComponent(String(v));

// A query that cannot be answered: the HTTP status the query API gives and the body it sends.
export class AnswerError extends Error {
  constructor(status, body) {
    super(body.error);
    this.status = status;
    this.body = body;
  }
}

const versionsUrl = (slug) => `${API}/${slug}/versions`;
const filesUrl = (slug, v) => `${SITE}/d/${slug}/${v ? `v/${v}/` : ''}`;
export const queryUrl = (slug, v, op, qs) =>
  `${API}/${slug}/versions/${v}/${op}${qs.length ? '?' + qs.join('&') : ''}`;

// The live datasets and their newest versions. An isolate serves one deployment, so the list is
// read once; an unread list is read again on the next call.
let liveList;
export async function live(env) {
  if (liveList || !env.ASSETS) {
    return liveList || {};
  }
  try {
    const r = await env.ASSETS.fetch(new Request(`${SITE}/latest.json`));
    if (r.ok) {
      liveList = await r.json();
    }
  } catch {
    // An unread list withholds nothing and names no newest version.
  }
  return liveList || {};
}
// For the tests: the isolate's list is read again.
export function reset() {
  liveList = undefined;
}

// A dataset the register has withheld keeps its loaded tables, files and cached answers until they
// are dropped, so the live list the site was built with decides, before anything cached is served.
export async function withheld(env, slug) {
  const latest = await live(env);
  return Object.keys(latest).length > 0 && !Object.hasOwn(latest, slug);
}

const missingTable = (e) => /no such table/i.test(String((e && e.message) || e));

// The D1 registry row for a version, or for the newest loaded version without one.
async function held(env, slug, version) {
  return version
    ? env.DB.prepare('SELECT * FROM _versions WHERE slug = ? AND version = ?')
        .bind(slug, version)
        .first()
    : env.DB.prepare('SELECT * FROM _versions WHERE slug = ? ORDER BY version DESC LIMIT 1')
        .bind(slug)
        .first();
}

const kept = (ctx, key, out) =>
  ctx.waitUntil &&
  ctx.waitUntil(
    caches.default.put(
      key,
      new Response(JSON.stringify(out), {
        headers: {
          'content-type': 'application/json',
          'cache-control': 'public, max-age=31536000, immutable',
        },
      }),
    ),
  );

// D1's answer, as the query API has always given it: rows in the order the version was loaded in.
// It is kept at the edge under the table it was read from, which a load of other rows renames, so
// the MCP tools, which do not pass through the API's own cache, are not sent to D1 again for it.
async function d1(ctx, slug, v, op, qs) {
  const key = new Request(
    `${SITE}/_d1/${enc(slug)}/${v.version}/${enc(v.tbl)}/${op}${qs.length ? '?' + qs.join('&') : ''}`,
  );
  const hit = ctx.waitUntil ? await caches.default.match(key) : undefined;
  if (hit) {
    return hit.json();
  }
  const params = new URLSearchParams(qs.join('&'));
  let plan;
  try {
    plan =
      op === 'rows'
        ? rowsQuery(v.tbl, JSON.parse(v.fields), params)
        : aggregateQuery(v.tbl, JSON.parse(v.fields), params);
  } catch (e) {
    if (e instanceof QueryError) {
      throw new AnswerError(400, { error: e.message });
    }
    throw e;
  }
  let rows;
  try {
    rows =
      (
        await ctx.env.DB.prepare(plan.sql)
          .bind(...plan.binds)
          .all()
      ).results || [];
  } catch (e) {
    throw new AnswerError(500, {
      error: 'The query could not run',
      detail: String(e.message || e).slice(0, 200),
    });
  }
  const more = rows.length > plan.limit;
  const header = JSON.parse(v.header || '{}');
  const out = {
    version: v.version,
    rows: more ? rows.slice(0, plan.limit) : rows,
    more,
    matched: null,
    cols: plan.cols,
    header,
    attribution: v.attribution ?? header.attribution ?? null,
    manifest: `${SITE}/d/${slug}/v/${v.version}/manifest.json`,
  };
  kept(ctx, key, out);
  return out;
}

const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');

// A version stored as parts carries the provenance of its newest part's file, with the version's
// own manifest for what belongs to the version: its date, its source and the hash of its bytes.
function partsHeader(part, meta, slug, v) {
  const h = { ...(part || {}) };
  const m = meta || {};
  h.version = m.version || v;
  h.url = `${SITE}/d/${slug}/v/${v}/`;
  if (m.as_at !== undefined && m.as_at !== null) {
    h.as_at = m.as_at;
  }
  const source = { ...(h.source || {}) };
  for (const [k, x] of [
    ['url', m.source && m.source.url],
    ['filename', m.filename],
    ['fetched_at', m.fetched_at],
    ['sha256', m.sha256],
    ['bytes', m.bytes],
    ['encoding', m.encoding],
    ['backfilled', m.backfilled],
  ]) {
    if (x !== undefined && x !== null) {
      source[k] = x;
    }
  }
  h.source = source;
  return h;
}

// A version stored as period parts is read part by part, listed with their rows by its manifest.
// A correction rewrites parts in place under the same keys and rewrites the manifest with its
// note, so an answer is kept under a digest of the manifest's text and that list.
async function fromParts(ctx, slug, v, op, qs, path, at) {
  const manifest = `${SITE}/d/${enc(slug)}/v/${v}/manifest.json`;
  const listed = [at.manifest.sha256, ...at.parts.map((p) => `${p.key} ${p.rows}`)].join('\n');
  const id = hex(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(listed))).slice(
    0,
    32,
  );
  const key = new Request(`${SITE}/_parquet/${ENGINE}/parts-${id}${path}`);
  const hit = await caches.default.match(key);
  if (hit) {
    return hit.json();
  }
  const r = await (op === 'rows' ? parquetRows : parquetAggregate)(
    ctx.env,
    at,
    new URLSearchParams(qs.join('&')),
    manifest,
  );
  const header = partsHeader(r.header, at.meta, slug, v);
  // parts names the periods read; the manifest gives each one's file, rows and provenance.
  const out = {
    version: v,
    rows: r.rows,
    more: r.more,
    matched: r.matched,
    cols: r.cols,
    header,
    attribution: at.attribution ?? header.attribution ?? null,
    parts: r.parts,
    manifest,
  };
  kept(ctx, key, out);
  return out;
}

async function fromFile(ctx, slug, v, op, qs, path, url, counted) {
  const at = await openVersion(ctx.env, slug, v);
  if (!at) {
    return null;
  }
  // A rollup is stamped with the identity of a version's data.parquet, so a version stored only as
  // parts has none to page by.
  if (!at.files.length) {
    return fromParts(ctx, slug, v, op, qs, path, at);
  }
  const entry = at.files[0];
  if (!entry.header) {
    throw new AnswerError(422, {
      error: `version ${v} of ${slug} carries no provenance in its file, so it is not answered here; its files are at ${filesUrl(slug, v)}`,
      files: filesUrl(slug, v),
    });
  }
  // The key holds the engine's version, so a fix is not hidden behind a year-long cached answer,
  // and the ETag of the file read, so an answer from a copy since written again is never served.
  const key = new Request(`${SITE}/_parquet/${ENGINE}/${enc(entry.etag)}${path}`);
  const hit = await caches.default.match(key);
  if (hit) {
    return hit.json();
  }
  const params = new URLSearchParams(qs.join('&'));
  let r;
  try {
    r = await (op === 'rows' ? parquetRows : parquetAggregate)(ctx.env, entry, params, url);
  } catch (e) {
    // A page refused for what counting every match would read is read only until it is full
    // when the version's rollup holds the count.
    const known =
      e instanceof BudgetError && entry.profiled && counted
        ? await counted(v).catch((x) => {
            // eslint-disable-next-line no-console -- the Workers log is where an operator sees this.
            console.error(`rollup ${slug} ${v}: ${x}`);
            return null;
          })
        : null;
    if (known === null) {
      throw e;
    }
    // A page still over the budget is refused with the cost of the full query.
    r = await parquetRows(ctx.env, entry, params, url, undefined, known).catch((x) => {
      throw x instanceof BudgetError ? e : x;
    });
  }
  const out = {
    version: v,
    rows: r.rows,
    more: r.more,
    matched: r.matched,
    cols: r.cols,
    header: entry.header,
    attribution: entry.header.attribution ?? null,
    file: url,
    manifest: `${SITE}/d/${enc(slug)}/v/${v}/manifest.json`,
  };
  // Rows without an order, and ties, come in the order of the file read. When that file is sorted,
  // the answer names its sort, so a caller can tell it from the publisher's order.
  if (op === 'rows' && entry.sortedBy && entry.sortedBy.length) {
    out.order = entry.sortedBy.map((c) => c.name);
  }
  // The bytes under one ETag never change, so the answer is kept as long as the edge keeps anything.
  kept(ctx, key, out);
  return out;
}

// A version's answer from its Parquet in R2, or null when R2 holds no table for it.
async function parquet(ctx, slug, v, op, qs, counted) {
  const path = `/api/v1/datasets/${enc(slug)}/versions/${v}/${op}${qs.length ? '?' + qs.join('&') : ''}`;
  const url = `${SITE}/d/${enc(slug)}/v/${v}/data.parquet`;
  try {
    try {
      return await fromFile(ctx, slug, v, op, qs, path, url, counted);
    } catch (e) {
      // The query copy was written again under this isolate's footer, so it is read afresh once.
      if (!(e instanceof StaleError)) {
        throw e;
      }
      forget(slug, v);
      return await fromFile(ctx, slug, v, op, qs, path, url, counted);
    }
  } catch (e) {
    if (e instanceof AnswerError) {
      throw e;
    }
    if (e instanceof BudgetError) {
      throw new AnswerError(422, { error: e.message, files: filesUrl(slug, v) });
    }
    if (e instanceof QueryError) {
      throw new AnswerError(400, { error: e.message });
    }
    if (e instanceof FileError) {
      throw new AnswerError(422, {
        error: `version ${v} of ${slug} is not answered here, since ${e.message}; its files are at ${filesUrl(slug, v)}`,
        files: filesUrl(slug, v),
      });
    }
    // eslint-disable-next-line no-console -- the Workers log is where an operator sees this.
    console.error(`parquet ${slug} ${v}: ${(e && e.stack) || e}`);
    throw new AnswerError(502, {
      error: `version ${v} of ${slug} could not be read; its files are at ${filesUrl(slug, v)}`,
      files: filesUrl(slug, v),
    });
  }
}

// The rows an unordered row query's filters match, from the version's rollup when a cube holds
// them, so a page the Parquet budget refuses only for counting every match is read until full.
function counter(ctx, slug, qs) {
  const filters = qs.filter((p) => !RESERVED.has(decodeURIComponent(p.split('=')[0])));
  return async (v) => {
    const c = await rollup(ctx, slug, v, 'aggregate', filters.concat(['metric=count']));
    return c ? c.matched : null;
  };
}

// Answers one query: rows or aggregate over the version named, else the newest live version.
// qs is the query's parameters as encoded `name=value` strings, without `format`. Returns
// { version, rows, more, matched, cols, header, attribution, query, manifest, file?, parts?,
// order? }, where matched is null when the engine did not count it, file or parts name what the
// Parquet engine read, and order names the sort of the file it read. Throws AnswerError.
export async function query(ctx, slug, want, op, qs) {
  const { env } = ctx;
  if (!SLUG.test(slug)) {
    throw new AnswerError(404, { error: 'No such dataset' });
  }
  if (want && !VERSION.test(want)) {
    throw new AnswerError(404, {
      error: 'A version is a date, YYYY-MM-DD',
      versions: versionsUrl(slug),
    });
  }
  if (!env.DB && !env.DIST) {
    throw new AnswerError(503, { error: NOT_ENABLED });
  }
  if (await withheld(env, slug)) {
    throw new AnswerError(410, { error: WITHHELD });
  }
  const v = want || (await live(env))[slug];
  const done = (out) => ({ ...out, query: queryUrl(slug, out.version, op, qs) });
  let row = null;
  if (env.DB) {
    try {
      row = await held(env, slug, v);
    } catch (e) {
      // A database never loaded holds nothing, and one that fails leaves the files to answer.
      if (!env.DIST) {
        throw missingTable(e)
          ? new AnswerError(503, { error: NOT_ENABLED })
          : new AnswerError(500, {
              error: 'The query could not run',
              detail: String(e.message || e).slice(0, 200),
            });
      }
      if (!missingTable(e)) {
        // eslint-disable-next-line no-console -- the Workers log is where an operator sees this.
        console.error(`d1 ${slug} ${v}: ${(e && e.stack) || e}`);
      }
    }
  }
  const inD1 = row && (!v || row.version === v) ? row : null;
  // A rollup holds the answer D1 gives, but breaks ties in a metric's order by its groups where D1
  // leaves them to SQLite. So for a version D1 holds it answers only a query whose order leaves no
  // ties, under the provenance D1 holds, and D1's answer to every query stays what it was.
  if (op === 'aggregate' && v && (!inD1 || determined(qs))) {
    const r = await rollup(ctx, slug, v, 'aggregate', qs);
    if (r) {
      if (inD1) {
        const header = JSON.parse(inD1.header || '{}');
        // It answers as D1 does, so nothing in the answer says which of the two counted it.
        const same = { ...r, header, attribution: inD1.attribution ?? header.attribution ?? null };
        delete same.file;
        return done(same);
      }
      return done(r);
    }
  }
  if (inD1) {
    return done(await d1(ctx, slug, inD1, op, qs));
  }
  // A query without a version that the newest version's file cannot answer is answered from the
  // newest version D1 holds, as it was before the files answered, and names that version.
  const fallback = async () => {
    if (want || !env.DB) {
      return null;
    }
    const newest = await held(env, slug).catch(() => null);
    return newest ? done(await d1(ctx, slug, newest, op, qs)) : null;
  };
  if (env.DIST && v) {
    let p;
    try {
      p = await parquet(
        ctx,
        slug,
        v,
        op,
        qs,
        op === 'rows' && !hasOrder(qs) && counter(ctx, slug, qs),
      );
    } catch (e) {
      const f = e instanceof AnswerError && e.status !== 400 ? await fallback() : null;
      if (f) {
        return f;
      }
      throw e;
    }
    if (p) {
      return done(p);
    }
  }
  const f = await fallback();
  if (f) {
    return f;
  }
  if (!v) {
    throw new AnswerError(404, {
      error: 'No such dataset in the query API',
      files: filesUrl(slug),
    });
  }
  throw new AnswerError(404, {
    error: `version ${v} of ${slug} has no table to query; ${versionsUrl(slug)} lists the versions that do`,
    versions: versionsUrl(slug),
    files: filesUrl(slug),
  });
}

const hasOrder = (qs) => qs.some((p) => p.split('=')[0] === 'order');
const param = (qs, k) => {
  const p = qs.find((x) => x.split('=')[0] === k);
  return p ? decodeURIComponent(p.slice(k.length + 1)) : '';
};
const names = (v) =>
  v
    .split(',')
    .map((x) => x.trim().split('.')[0])
    .filter(Boolean);
// Whether an aggregate's order leaves no ties: no order, which D1 gives by group, or one that
// names every group field.
function determined(qs) {
  const order = names(param(qs, 'order'));
  const group = names(param(qs, 'group'));
  return !order.length || group.every((g) => order.includes(g));
}
