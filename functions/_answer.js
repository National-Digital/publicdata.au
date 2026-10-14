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
// A failure of D1 is logged with its own text. A query D1 fails to run with no files to fall back
// on is answered as the query API always answered it, with the start of that text as `detail`;
// every other failure, which the API did not answer before, leaves the text out.
const FAILED = 'The query could not run';
function d1Error(slug, version, e, detail = false) {
  // eslint-disable-next-line no-console -- the Workers log is where an operator sees this.
  console.error(`d1 ${slug} ${version}: ${(e && e.stack) || e}`);
  return new AnswerError(
    500,
    detail
      ? { error: FAILED, detail: String((e && e.message) || e).slice(0, 200) }
      : { error: FAILED },
  );
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
  provenances.clear();
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
    throw d1Error(slug, v.version, e, !ctx.env.DIST);
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

// A header's keys in the order provenance.header writes them, and those that are the dataset's
// rather than the version's (d1.DATASET_KEYS). Places and the field count are the version's.
const HEADER_KEYS = [
  'site',
  'operator',
  'dataset',
  'title',
  'version',
  'as_at',
  'url',
  'publisher',
  'licence',
  'attribution',
  'cite',
  'cite_request',
  'source',
  'places',
  'rows',
  'fields',
  'kind',
  'tables',
  'not_endorsed',
];
const DATASET_KEYS = new Set([
  'site',
  'operator',
  'dataset',
  'title',
  'publisher',
  'licence',
  'cite_request',
  'kind',
  'tables',
  'not_endorsed',
]);

// A version stored as parts answers with the header D1 loads it with (d1.parts_header). What is
// the version's comes from its manifest, and what is the dataset's from `dataset`, the part of that
// header fields.json gives from the register this deployment was built with. A finished part can
// be an earlier version's file, written under the register as it was then, so the newest part's
// own header stands in for the dataset's part only when fields.json gives none.
export function partsHeader(part, at, slug, v, dataset = null) {
  const m = at.meta || {};
  const version = m.version || v;
  const page = `${SITE}/d/${slug}/v/${version}/`;
  const from = dataset || part || {};
  const attribution = at.attribution ?? (part && part.attribution) ?? null;
  const own = {
    version,
    // provenance.header writes an empty as_at as null.
    as_at: m.as_at || null,
    url: `${page}data.duckdb`,
    attribution,
    source: {
      url: (m.source && m.source.url) ?? null,
      filename: m.filename ?? null,
      fetched_at: m.fetched_at ?? null,
      sha256: m.sha256 ?? null,
      bytes: m.bytes ?? null,
      encoding: m.encoding ?? null,
      backfilled: m.backfilled ?? null,
    },
    rows: at.rows,
  };
  // A version's places and field count are its own, as d1.as_built reads them from its manifest.
  const pins = m.places || [];
  if (pins.length) {
    own.places = {
      datasets: pins.map((p) => p.dataset),
      attribution:
        (dataset && dataset.places_attribution) || (from.places && from.places.attribution) || null,
    };
  }
  if (m.fields !== null && m.fields !== undefined) {
    own.fields = m.fields;
  } else if (Object.hasOwn(from, 'fields')) {
    own.fields = from.fields;
  }
  if (attribution !== null) {
    const by = (from.operator && from.operator.name) || 'National Digital';
    own.cite = `${attribution} Serialised and versioned by ${by} at publicdata.au, ${page}`;
  }
  const h = {};
  for (const k of HEADER_KEYS) {
    if (Object.hasOwn(own, k)) {
      h[k] = own[k];
    } else if (DATASET_KEYS.has(k) && Object.hasOwn(from, k)) {
      h[k] = from[k];
    }
  }
  return h;
}

// The dataset's part of a version's header, from its fields.json, read once per isolate.
// A field list that could not be read is tried again after a minute, not kept for the isolate.
const provenances = new Map();
export const RETRY = { ms: 60_000 };
async function datasetHeader(env, slug) {
  const hit = provenances.get(slug);
  if (hit && (hit.value || Date.now() - hit.at < RETRY.ms)) {
    return hit.value;
  }
  let out = null;
  try {
    const r = env.ASSETS && (await env.ASSETS.fetch(new Request(`${SITE}/d/${slug}/fields.json`)));
    if (r && r.ok) {
      const f = await r.json();
      out = f.provenance && typeof f.provenance === 'object' ? f.provenance : null;
    }
  } catch {
    out = null;
  }
  provenances.set(slug, { value: out, at: Date.now() });
  return out;
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
  if (!r.header) {
    throw new AnswerError(422, {
      error: `version ${v} of ${slug} carries no provenance in its parts, so it is not answered here; its files are at ${filesUrl(slug, v)}`,
      files: filesUrl(slug, v),
    });
  }
  const header = partsHeader(r.header, at, slug, v, await datasetHeader(ctx.env, slug));
  // parts names the periods read; the manifest gives each one's file, rows and provenance.
  const out = {
    version: v,
    rows: r.rows,
    more: r.more,
    matched: r.matched,
    cols: r.cols,
    header,
    attribution: header.attribution,
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
  const filters = qs.filter((p) => !RESERVED.has(new URLSearchParams(p).keys().next().value));
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
        throw missingTable(e) ? new AnswerError(503, { error: NOT_ENABLED }) : d1Error(slug, v, e);
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
  // A failure of D1 on the version it holds, kept so that the files never hide it.
  let failed = null;
  if (inD1) {
    try {
      return done(await d1(ctx, slug, inD1, op, qs));
    } catch (e) {
      // A query D1 fails to run is answered from the files when R2 is there to read them.
      if (!(e instanceof AnswerError) || e.status !== 500 || !env.DIST || !v) {
        throw e;
      }
      failed = e;
    }
  }
  // When the files cannot answer what D1 failed, the answer is D1's failure, naming both.
  const d1Failed = (why) => {
    if (!failed) {
      return null;
    }
    return new AnswerError(500, {
      ...failed.body,
      files_error: why || `version ${v} of ${slug} has no file to answer it from`,
    });
  };
  // A query without a version that the newest version's file cannot answer is answered from the
  // newest version D1 holds, as it was before the files answered, and names that version.
  const fallback = async () => {
    if (want || !env.DB || failed) {
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
      throw (e instanceof AnswerError && e.status !== 400 && d1Failed(e.body.error)) || e;
    }
    if (p) {
      return done(p);
    }
  }
  if (failed) {
    throw d1Failed();
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
// URLSearchParams decodes as the engines do, leaving a malformed escape for them to refuse.
const param = (qs, k) => new URLSearchParams(qs.join('&')).get(k) ?? '';
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
