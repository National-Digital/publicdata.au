// One query, whichever engine answers it. The query API and the MCP row tools both ask here, so a
// query gives the same rows, in the same order, from every engine and through either door. A
// version's rollup answers first where it holds the answer exactly, then D1 for the versions it
// holds, then the version's Parquet in R2 for every other version.

import { SLUG } from './_lib.js';
import { QueryError, aggregateQuery, rowsQuery } from './_query.js';
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

// The live datasets and their newest versions, and each dataset's field list. An isolate serves
// one deployment, so each is read once; an unread list is read again on the next call.
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
const orders = new Map();
// The columns a version's query copy is ordered by before the source position, as fields.json
// gives them (the entry's sort, then its key), or null when that is not known.
async function fileOrder(env, slug) {
  if (orders.has(slug)) {
    return orders.get(slug);
  }
  let out = null;
  if (env.ASSETS) {
    try {
      const r = await env.ASSETS.fetch(new Request(`${SITE}/d/${slug}/fields.json`));
      if (r.ok) {
        const f = await r.json();
        out = Array.isArray(f.order) ? f.order : null;
      }
    } catch {
      out = null;
    }
  }
  orders.set(slug, out);
  return out;
}
// For the tests: the isolate's lists are read again.
export function reset() {
  liveList = undefined;
  orders.clear();
}

const missingTable = (e) => /no such table/i.test(String((e && e.message) || e));

// The D1 registry row for a version, or the newest loaded version without one, with the order its
// rows were loaded in. Null when D1 does not hold it.
async function held(env, slug, version) {
  const where = version ? 'v.slug = ? AND v.version = ?' : 'v.slug = ?';
  const binds = version ? [slug, version] : [slug];
  const tail = ' ORDER BY v.version DESC LIMIT 1';
  try {
    return await env.DB.prepare(
      `SELECT v.*, o.ord AS ord FROM _versions v LEFT JOIN _orders o ON o.slug = v.slug AND o.version = v.version WHERE ${where}${tail}`,
    )
      .bind(...binds)
      .first();
  } catch (e) {
    if (!missingTable(e) || /_versions/.test(String(e.message || e))) {
      throw e;
    }
  }
  // A database loaded before the order was recorded has no _orders table.
  return env.DB.prepare(`SELECT v.* FROM _versions v WHERE ${where}${tail}`)
    .bind(...binds)
    .first();
}

// D1's rowid is the row's place in the data.parquet it was loaded from. When that file is in
// another order than the query copy the Parquet engine reads, the copy's order is asked for, so
// both engines give the same rows in the same order.
async function d1(ctx, slug, v, op, params) {
  let tail = [];
  if (op === 'rows' && v.ord !== undefined && v.ord !== null) {
    const order = await fileOrder(ctx.env, slug);
    if (order && v.ord !== order.join(',')) {
      tail = order;
    }
  }
  let plan;
  try {
    plan =
      op === 'rows'
        ? rowsQuery(v.tbl, JSON.parse(v.fields), params, tail)
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
  return {
    version: v.version,
    rows: more ? rows.slice(0, plan.limit) : rows,
    more,
    matched: null,
    cols: plan.cols,
    offset: plan.offset,
    limit: plan.limit,
    header,
    attribution: v.attribution ?? header.attribution ?? null,
    manifest: `${SITE}/d/${slug}/v/${v.version}/manifest.json`,
  };
}

const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');
const kept = (ctx, key, out) =>
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
  // parts names the periods read; the manifest gives each one's file, rows and provenance.
  const out = {
    version: v,
    rows: r.rows,
    more: r.more,
    matched: r.matched,
    cols: r.cols,
    header: r.header || {},
    attribution: at.attribution ?? r.attribution ?? null,
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
  // The bytes under one ETag never change, so the answer is kept as long as the edge keeps anything.
  kept(ctx, key, out);
  return out;
}

// A version's answer from its Parquet in R2, or null when R2 holds no file for it.
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

// Answers one query: rows or aggregate over the version named, else the newest live version.
// qs is the query's parameters as encoded `name=value` strings, without `format`. Returns
// { version, rows, more, matched, cols, header, attribution, query, file?, parts?, manifest },
// where matched is null when the engine did not count it, and file or parts name what the
// Parquet engine read. Throws AnswerError.
// `counted(version)` gives the rows a filter matches, for a page the budget refuses only because
// counting them all reads too much.
export async function query(ctx, slug, want, op, qs, counted) {
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
  // A dataset the register has withheld keeps its loaded tables and files until they are dropped,
  // so the live list the site was built with decides.
  const latest = await live(env);
  if (Object.keys(latest).length && !Object.hasOwn(latest, slug)) {
    throw new AnswerError(410, { error: WITHHELD });
  }
  const v = want || latest[slug];
  const params = () => new URLSearchParams(qs.join('&'));
  const done = (out) => ({ ...out, query: queryUrl(slug, out.version, op, qs) });
  if (op === 'aggregate' && v) {
    const r = await rollup(ctx, slug, v, 'aggregate', qs);
    if (r) {
      return done(r);
    }
  }
  let row = null;
  if (env.DB) {
    try {
      row = await held(env, slug, v);
    } catch (e) {
      if (!missingTable(e)) {
        throw e;
      }
      // A bound database that was never loaded holds nothing.
      if (!env.DIST) {
        throw new AnswerError(503, { error: NOT_ENABLED });
      }
    }
    if (row && (!v || row.version === v)) {
      return done(await d1(ctx, slug, row, op, params()));
    }
  }
  if (env.DIST && v) {
    const p = await parquet(ctx, slug, v, op, qs, counted);
    if (p) {
      return done(p);
    }
  }
  // The newest version has no file the engine can read, so the newest version D1 holds answers.
  if (!want && env.DB) {
    const newest = await held(env, slug).catch(() => null);
    if (newest) {
      return done(await d1(ctx, slug, newest, op, params()));
    }
  }
  if (!v && !row) {
    throw new AnswerError(404, {
      error: 'No such dataset in the query API',
      files: filesUrl(slug),
    });
  }
  throw new AnswerError(404, {
    error: `${slug} has no version ${v}; get_dataset lists its versions, as ${versionsUrl(slug)} does`,
    versions: versionsUrl(slug),
    files: filesUrl(slug),
  });
}
