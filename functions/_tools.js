import { AnswerError, query } from './_answer.js';
import { onRequestGet as dFile } from './d/[[path]].js';
import { onRequestPost as castVote } from './api/v1/votes/[slug].js';
import { onRequestGet as voteCounts } from './api/v1/votes.js';
import { onRequestGet as catalogue } from './api/v1/catalogue.js';
import { onRequestGet as searchDatasets } from './api/v1/datasets.js';

// The server side of the tools in pipeline/publicdata/api.json. Each does what its twin in
// site.js does, through the same handlers the public URLs use, so answers and caching match.
const SITE = 'https://publicdata.au';

export class ToolError extends Error {}

const enc = (v) => encodeURIComponent(String(v));
const text = (v) => (typeof v === 'string' ? v : JSON.stringify(v));

// The same mapping as filters() in site.js; functions/_mcp.test.mjs holds the two to each other.
export function filters(where) {
  const out = [];
  for (const k of Object.keys(where || {})) {
    const w = where[k],
      f = enc(k);
    if (w === null) {
      out.push(f + '=is.null');
    } else if (Array.isArray(w)) {
      if (w.some((x) => String(x).indexOf(',') >= 0)) {
        throw new ToolError(
          'a list value cannot contain a comma; filter ' + k + ' on one value at a time',
        );
      }
      out.push(f + '=in.' + enc('(' + w.join(',') + ')'));
    } else if (typeof w === 'object') {
      if (w.min !== undefined) {
        out.push(f + '=gte.' + enc(w.min));
      }
      if (w.max !== undefined) {
        out.push(f + '=lte.' + enc(w.max));
      }
      if (w.like !== undefined) {
        out.push(f + '=ilike.' + enc(w.like));
      }
    } else {
      out.push(f + '=eq.' + enc(w));
    }
  }
  return out;
}

async function file(ctx, path) {
  const req = new Request(SITE + path);
  // /d/ reads fall through to R2 as the public URL does; everything else is a static asset.
  const r = path.startsWith('/d/')
    ? await dFile({ request: req, env: ctx.env })
    : await ctx.env.ASSETS.fetch(req);
  if (!r.ok) {
    throw new ToolError(r.status + ' for ' + SITE + path);
  }
  return r.json();
}

const VERSION = /^\d{4}-\d{2}-\d{2}$/;

// The query API's own answer, through the same query path (functions/_answer.js), so a tool and
// the URL it cites give the same rows from whichever engine holds the version.
async function ask(ctx, slug, version, op, qs) {
  if (version !== undefined && !VERSION.test(version)) {
    throw new ToolError('version is a date, YYYY-MM-DD, from get_dataset');
  }
  try {
    return await query(ctx, slug, version, op, qs);
  } catch (e) {
    if (e instanceof AnswerError) {
      throw new ToolError(e.body.error);
    }
    throw e;
  }
}

async function total(ctx, slug, version, where) {
  const c = await ask(ctx, slug, version, 'aggregate', filters(where).concat(['metric=count']));
  return c.rows[0] ? c.rows[0].count : 0;
}

function slugOf(input) {
  if (!input.slug) {
    throw new ToolError('slug is required; find one with search_datasets');
  }
  return input.slug;
}

export const EXEC = {};
EXEC.search_datasets = async (ctx, input) => {
  const r = await searchDatasets({
    request: new Request(SITE + '/api/v1/datasets?q=' + enc(input.query || ''), {
      headers: ctx.request.headers,
    }),
    env: ctx.env,
  });
  const b = await r.json();
  if (!r.ok) {
    throw new ToolError(b.error || String(r.status));
  }
  return text(b);
};
EXEC.get_dataset = async (ctx, input) => {
  const base = '/d/' + enc(slugOf(input)) + '/';
  const [datapackage, versions] = await Promise.all([
    file(ctx, base + 'datapackage.json'),
    file(ctx, base + 'versions.json'),
  ]);
  return text({ datapackage, versions });
};
EXEC.list_fields = async (ctx, input) => {
  const slug = slugOf(input);
  try {
    return text(await file(ctx, '/d/' + enc(slug) + '/fields.json'));
  } catch (e) {
    if (e instanceof ToolError) {
      throw new ToolError(
        'no queryable dataset with slug ' + slug + '; search_datasets names them',
      );
    }
    throw e;
  }
};
EXEC.list_partitions = async (ctx, input) => {
  const slug = slugOf(input);
  const latest = await file(ctx, '/latest.json');
  if (!latest[slug]) {
    throw new ToolError('no live dataset with slug ' + slug);
  }
  const base = '/d/' + enc(slug) + '/v/' + latest[slug] + '/';
  const ix = await file(ctx, base + 'by/' + enc(input.field) + '/index.json');
  return text({
    field: ix.field,
    version_base: SITE + base,
    partitions: ix.partitions.map((e) => ({
      value: e.value,
      rows: e.rows,
      url: SITE + base + e.json,
    })),
  });
};
EXEC.query_rows = async (ctx, input) => {
  const limit = input.limit || 50,
    offset = input.offset || 0,
    slug = slugOf(input);
  const qs = filters(input.where).concat(['limit=' + limit, 'offset=' + offset]);
  if (input.select && input.select.length) {
    qs.push('select=' + input.select.map(enc).join(','));
  }
  if (input.order) {
    qs.push('order=' + enc(input.order));
  }
  const a = await ask(ctx, slug, input.version, 'rows', qs);
  return text({
    version: a.version,
    rows: a.rows,
    matched: a.matched ?? (await total(ctx, slug, a.version, input.where)),
    next_offset: a.more ? offset + limit : null,
    query: a.query,
    attribution: a.attribution,
    file: a.file,
    parts: a.parts,
    order: a.order,
    manifest: a.manifest,
  });
};
EXEC.count_rows = async (ctx, input) => {
  const metric = input.metric || 'count',
    limit = input.limit || 100,
    group = input.group_by || [],
    slug = slugOf(input);
  const alias = metric === 'count' ? 'count' : metric.replace('.', '_');
  // Equal totals come in group order, so the top groups are the same from every engine.
  const order = [alias + '.desc', ...group.map((g) => g + '.asc')].map(enc).join(',');
  const qs = filters(input.where).concat([
    'metric=' + enc(metric),
    'order=' + order,
    'limit=' + limit,
  ]);
  if (group.length) {
    qs.push('group=' + group.map(enc).join(','));
  }
  const a = await ask(ctx, slug, input.version, 'aggregate', qs);
  return text({
    version: a.version,
    group_by: group,
    metric,
    groups: a.rows,
    truncated: a.more,
    matched: a.matched ?? (await total(ctx, slug, a.version, input.where)),
    query: a.query,
    attribution: a.attribution,
    file: a.file,
    parts: a.parts,
    manifest: a.manifest,
  });
};
EXEC.diff_versions = async (ctx, input) =>
  text(
    await file(
      ctx,
      '/d/' + enc(slugOf(input)) + '/diff/' + enc(input.from) + '..' + enc(input.to) + '.json',
    ),
  );
EXEC.search_catalogue = async (ctx, input) => {
  const qs = new URLSearchParams({ q: input.query || '', limit: '20' });
  if (input.jurisdiction) {
    qs.set('jur', input.jurisdiction);
  }
  if (input.votable_only) {
    qs.set('state', 'votable,chosen');
  }
  if (input.offset) {
    qs.set('offset', String(input.offset));
  }
  const r = await catalogue({
    request: new Request(SITE + '/api/v1/catalogue?' + qs, { headers: ctx.request.headers }),
    env: ctx.env,
  });
  const b = await r.json();
  if (!r.ok) {
    throw new ToolError(b.error || String(r.status));
  }
  return text(b);
};
// The same shape as backlog() in site.js; functions/_mcp.test.mjs holds the two to each other.
export function backlog(register, counts) {
  const byVotes = (a, b) => b.votes - a.votes || ((a.slug || a.vote) < (b.slug || b.vote) ? -1 : 1);
  const known = new Set(register.entries.map((e) => e.slug));
  const entries = register.entries
    .filter((e) => e.status !== 'live')
    .map((e) => ({
      slug: e.slug,
      title: e.title,
      publisher: e.publisher ? e.publisher.name : null,
      status: e.status,
      votes: counts[e.slug] || 0,
      summary: e.summary ?? null,
      source: e.source ?? null,
      blocked_reason: e.blocked_reason ?? null,
    }));
  const catalogue_votes = Object.keys(counts)
    .filter((k) => !known.has(k))
    .map((vote) => ({ vote, votes: counts[vote] }));
  return { entries: entries.sort(byVotes), catalogue_votes: catalogue_votes.sort(byVotes) };
}
EXEC.list_backlog = async (ctx) => {
  const [register, counts] = await Promise.all([
    file(ctx, '/backlog.json'),
    voteCounts({ env: ctx.env, waitUntil: ctx.waitUntil }).then((r) => r.json()),
  ]);
  return text(backlog(register, counts));
};
EXEC.upvote_dataset = async (ctx, input) => {
  // The caller's own request goes through, so the one-vote-a-day hash is theirs.
  const r = await castVote({ request: ctx.request, env: ctx.env, params: { slug: slugOf(input) } });
  const b = await r.json();
  if (!r.ok) {
    throw new ToolError(b.error || String(r.status));
  }
  return text(b);
};
