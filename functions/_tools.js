import { answer } from './_api.js';
import { aggregateQuery, rowsQuery } from './_query.js';
import { rollup } from './_rollup.js';
import { onRequestGet as dFile } from './d/[[path]].js';
import { onRequestPost as castVote } from './api/v1/votes/[slug].js';
import { onRequestGet as voteCounts } from './api/v1/votes.js';
import { onRequestGet as catalogue } from './api/v1/catalogue.js';
import { onRequestGet as searchDatasets } from './api/v1/datasets.js';

// The server side of the tools in pipeline/publicdata/api.json. Each does what its twin in
// site.js does, through the same handlers the public URLs use, so answers and caching match.
const SITE = 'https://publicdata.au';
const API = '/api/v1/datasets/';

export class ToolError extends Error {}

const enc = (v) => encodeURIComponent(String(v));
const text = (v) => (typeof v === 'string' ? v : JSON.stringify(v));

// The same mapping as filters() in site.js; functions/_mcp.test.mjs holds the two to each other.
export function filters(where) {
  const out = [];
  for (const k of Object.keys(where || {})) {
    const w = where[k], f = enc(k);
    if (w === null) out.push(f + '=is.null');
    else if (Array.isArray(w)) {
      if (w.some((x) => String(x).indexOf(',') >= 0)) throw new ToolError('a list value cannot contain a comma; filter ' + k + ' on one value at a time');
      out.push(f + '=in.' + enc('(' + w.join(',') + ')'));
    } else if (typeof w === 'object') {
      if (w.min !== undefined) out.push(f + '=gte.' + enc(w.min));
      if (w.max !== undefined) out.push(f + '=lte.' + enc(w.max));
      if (w.like !== undefined) out.push(f + '=ilike.' + enc(w.like));
    } else out.push(f + '=eq.' + enc(w));
  }
  return out;
}

async function file(ctx, path) {
  const req = new Request(SITE + path);
  // /d/ reads fall through to R2 as the public URL does; everything else is a static asset.
  const r = path.startsWith('/d/') ? await dFile({ request: req, env: ctx.env }) : await ctx.env.ASSETS.fetch(req);
  if (!r.ok) throw new ToolError(r.status + ' for ' + SITE + path);
  return r.json();
}

async function api(ctx, slug, op, version, qs) {
  const u = API + enc(slug) + '/' + (version ? 'versions/' + enc(version) + '/' : '') + op + (qs.length ? '?' + qs.join('&') : '');
  const context = { request: new Request(SITE + u), env: ctx.env, params: { slug, version }, waitUntil: ctx.waitUntil };
  const r = await answer(context, op === 'rows' ? rowsQuery : aggregateQuery, op);
  const b = await r.json().catch(() => ({}));
  if (!r.ok) throw new ToolError(b.error || r.status + ' for ' + SITE + u);
  b.version = r.headers.get('x-publicdata-version');
  b.url = SITE + u;
  return b;
}

async function total(ctx, slug, version, where) {
  const b = await api(ctx, slug, 'aggregate', version, filters(where));
  return b.rows[0] ? b.rows[0].count : 0;
}

function slugOf(input) {
  if (!input.slug) throw new ToolError('slug is required; find one with search_datasets');
  return input.slug;
}

export const EXEC = {};
EXEC.search_datasets = async (ctx, input) => {
  const r = await searchDatasets({ request: new Request(SITE + '/api/v1/datasets?q=' + enc(input.query || ''), { headers: ctx.request.headers }), env: ctx.env });
  const b = await r.json();
  if (!r.ok) throw new ToolError(b.error || String(r.status));
  return text(b);
};
EXEC.get_dataset = async (ctx, input) => {
  const base = '/d/' + enc(slugOf(input)) + '/';
  const [datapackage, versions] = await Promise.all([file(ctx, base + 'datapackage.json'), file(ctx, base + 'versions.json')]);
  return text({ datapackage, versions });
};
EXEC.list_fields = async (ctx, input) => {
  const slug = slugOf(input);
  try {
    return text(await file(ctx, '/d/' + enc(slug) + '/fields.json'));
  } catch (e) {
    if (e instanceof ToolError) throw new ToolError('no queryable dataset with slug ' + slug + '; search_datasets names them');
    throw e;
  }
};
EXEC.list_partitions = async (ctx, input) => {
  const slug = slugOf(input);
  const latest = await file(ctx, '/latest.json');
  if (!latest[slug]) throw new ToolError('no live dataset with slug ' + slug);
  const base = '/d/' + enc(slug) + '/v/' + latest[slug] + '/';
  const ix = await file(ctx, base + 'by/' + enc(input.field) + '/index.json');
  return text({ field: ix.field, version_base: SITE + base, partitions: ix.partitions.map((e) => ({ value: e.value, rows: e.rows, url: SITE + base + e.json })) });
};
EXEC.query_rows = async (ctx, input) => {
  const limit = input.limit || 50, offset = input.offset || 0, slug = slugOf(input);
  const qs = filters(input.where).concat(['limit=' + limit, 'offset=' + offset]);
  if (input.select && input.select.length) qs.push('select=' + input.select.map(enc).join(','));
  if (input.order) qs.push('order=' + enc(input.order));
  const b = await api(ctx, slug, 'rows', input.version, qs);
  const n = await total(ctx, slug, b.version, input.where);
  return text({ version: b.version, rows: b.rows, matched: n, next_offset: b.next ? offset + limit : null, query: b.url, attribution: b.publicdata && b.publicdata.attribution });
};
EXEC.count_rows = async (ctx, input) => {
  const metric = input.metric || 'count', limit = input.limit || 100, group = input.group_by || [], slug = slugOf(input);
  const alias = metric === 'count' ? 'count' : metric.replace('.', '_');
  // Equal totals come in group order, so the top groups are the same from every engine.
  const order = [alias + '.desc', ...group.map((g) => g + '.asc')].map(enc).join(',');
  const qs = filters(input.where).concat(['metric=' + enc(metric), 'order=' + order, 'limit=' + limit]);
  if (group.length) qs.push('group=' + group.map(enc).join(','));
  const r = await rollup(ctx, slug, input.version, 'aggregate', qs);
  if (r) return text({ version: r.version, group_by: group, metric, groups: r.rows, truncated: r.more, matched: r.matched, query: r.query, attribution: r.attribution });
  const b = await api(ctx, slug, 'aggregate', input.version, qs);
  const n = await total(ctx, slug, b.version, input.where);
  return text({ version: b.version, group_by: group, metric, groups: b.rows, truncated: !!b.next, matched: n, query: b.url, attribution: b.publicdata && b.publicdata.attribution });
};
EXEC.diff_versions = async (ctx, input) => text(await file(ctx, '/d/' + enc(slugOf(input)) + '/diff/' + enc(input.from) + '..' + enc(input.to) + '.json'));
EXEC.search_catalogue = async (ctx, input) => {
  const qs = new URLSearchParams({ q: input.query || '', limit: '20' });
  if (input.jurisdiction) qs.set('jur', input.jurisdiction);
  if (input.votable_only) qs.set('state', 'votable,chosen');
  if (input.offset) qs.set('offset', String(input.offset));
  const r = await catalogue({ request: new Request(SITE + '/api/v1/catalogue?' + qs, { headers: ctx.request.headers }), env: ctx.env });
  const b = await r.json();
  if (!r.ok) throw new ToolError(b.error || String(r.status));
  return text(b);
};
// The same shape as backlog() in site.js; functions/_mcp.test.mjs holds the two to each other.
export function backlog(register, counts) {
  const byVotes = (a, b) => b.votes - a.votes || ((a.slug || a.vote) < (b.slug || b.vote) ? -1 : 1);
  const known = new Set(register.entries.map((e) => e.slug));
  const entries = register.entries.filter((e) => e.status !== 'live').map((e) => ({
    slug: e.slug, title: e.title, publisher: e.publisher ? e.publisher.name : null, status: e.status, votes: counts[e.slug] || 0,
    summary: e.summary ?? null, source: e.source ?? null, blocked_reason: e.blocked_reason ?? null,
  }));
  const catalogue_votes = Object.keys(counts).filter((k) => !known.has(k)).map((vote) => ({ vote, votes: counts[vote] }));
  return { entries: entries.sort(byVotes), catalogue_votes: catalogue_votes.sort(byVotes) };
}
EXEC.list_backlog = async (ctx) => {
  const [register, counts] = await Promise.all([file(ctx, '/backlog.json'), voteCounts({ env: ctx.env, waitUntil: ctx.waitUntil }).then((r) => r.json())]);
  return text(backlog(register, counts));
};
EXEC.upvote_dataset = async (ctx, input) => {
  // The caller's own request goes through, so the one-vote-a-day hash is theirs.
  const r = await castVote({ request: ctx.request, env: ctx.env, params: { slug: slugOf(input) } });
  const b = await r.json();
  if (!r.ok) throw new ToolError(b.error || String(r.status));
  return text(b);
};
