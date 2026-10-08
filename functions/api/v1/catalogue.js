import { json } from '../../_lib.js';
import { CatalogueError, resolve, searchPlan, table } from '../../_catalogue.js';
import { policy, spend } from '../../_limit.js';

const SITE = 'https://publicdata.au';
const COLS = [
  'id',
  'title',
  'summary',
  'publisher',
  'publisher_path',
  'jur',
  'url',
  'licence',
  'formats',
  'modified',
  'state',
  'vote',
  'note',
];

export function shape(r) {
  const o = {};
  for (const c of COLS) o[c] = r[c];
  o.publisher_page = SITE + r.publisher_path;
  if (r.state === 'served') o.page = SITE + r.note;
  o.reason = r.state === 'closed' ? r.note : null;
  delete o.note;
  return o;
}

// Every dataset on the portals read: search by words, government and state, look up ids, or
// find the record a portal URL names.
export async function onRequestGet({ request, env }) {
  const params = new URL(request.url).searchParams;
  const wait = await spend(
    request,
    params.has('q') && !params.has('url') && !params.has('ids') ? 3 : 2,
  );
  if (wait)
    return json({ error: `Too many requests from this address. Wait ${wait} seconds.` }, 429, {
      'retry-after': String(wait),
      'ratelimit-policy': policy(),
    });
  const t = await table(env);
  if (!t)
    return json(
      {
        error:
          'The catalogue search is not loaded yet. Browse by government at https://publicdata.au/browse/',
      },
      503,
    );
  const cache = { 'cache-control': 'public, max-age=300', 'ratelimit-policy': policy() };
  const head = { catalogue_read: t.version, records: t.rows };
  try {
    if (params.has('url')) {
      const r = await resolve(env, t.tbl, params.get('url'));
      return json({ ...head, match: r ? shape(r) : null }, 200, cache);
    }
    if (params.has('ids')) {
      const ids = params.get('ids').split(',').filter(Boolean).slice(0, 50);
      if (!ids.length) return json({ ...head, rows: [] }, 200, cache);
      const res = await env.DB.prepare(
        `SELECT * FROM "${t.tbl}" WHERE id IN (${ids.map(() => '?').join(',')}) OR vote IN (${ids.map(() => '?').join(',')})`,
      )
        .bind(...ids, ...ids)
        .all();
      return json({ ...head, rows: (res.results || []).map(shape) }, 200, cache);
    }
    const plan = searchPlan(t.tbl, params);
    // The total is counted on the first page only; later pages carry next_offset alone.
    const [rows, n] = await Promise.all([
      env.DB.prepare(plan.sql)
        .bind(...plan.binds)
        .all(),
      plan.offset
        ? null
        : env.DB.prepare(plan.count)
            .bind(...plan.binds)
            .first(),
    ]);
    const out = (rows.results || []).map(shape);
    const more = out.length > plan.limit;
    const total = plan.offset ? {} : { total: n ? n.n : 0 };
    return json(
      {
        ...head,
        ...total,
        rows: out.slice(0, plan.limit),
        next_offset: more ? plan.offset + plan.limit : null,
      },
      200,
      cache,
    );
  } catch (e) {
    if (e instanceof CatalogueError) return json({ error: e.message }, 400);
    throw e;
  }
}
