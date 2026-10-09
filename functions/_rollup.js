// Answers a count from a version's rollup: its counts and totals grouped over the fields readers
// filter and group on, written by the deploy to R2 under _rollup/. A query the rollup cannot
// answer exactly returns null, and the caller asks the next engine. Filters, nulls, LIKE and
// ordering follow SQLite, so an answer here is the one the query API gives.

import { gunzip } from './_lib.js';

const SITE = 'https://publicdata.au';
const API = `${SITE}/api/v1/datasets`;
const VERSION = /^\d{4}-\d{2}-\d{2}$/;
const LIMIT_DEFAULT = 100;
const LIMIT_MAX = 10000;
const MAX_PARAMS = 90;
const OPS = new Set(['eq', 'neq', 'gt', 'gte', 'lt', 'lte']);
const FNS = new Set(['count', 'sum', 'avg', 'min', 'max']);
const RESERVED = new Set(['select', 'order', 'limit', 'offset', 'format', 'group', 'metric']);
const KEEP = 4;

export const key = (slug, version) => `_rollup/${slug}/${version}.json.gz`;

// SQLite orders NULL, then numbers, then text by its UTF-8 bytes, which is code point order.
function cmpText(a, b) {
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) {
    const x = a.codePointAt(i),
      y = b.codePointAt(i);
    if (x !== y) {
      return x < y ? -1 : 1;
    }
    if (x > 0xffff) {
      i++;
    }
  }
  return a.length === b.length ? 0 : a.length < b.length ? -1 : 1;
}

export function cmp(a, b) {
  if (a === b) {
    return 0;
  }
  if (a === null || a === undefined) {
    return -1;
  }
  if (b === null || b === undefined) {
    return 1;
  }
  const sa = typeof a === 'string',
    sb = typeof b === 'string';
  if (sa !== sb) {
    return sa ? 1 : -1;
  }
  return sa ? cmpText(a, b) : a < b ? -1 : a > b ? 1 : 0;
}

const lowerAscii = (s) => s.replace(/[A-Z]/g, (c) => c.toLowerCase());

// A pattern whose only wildcard is *, matched without backtracking: the first piece anchors the
// start, the last the end, and each piece between is taken at its earliest place, which is
// always safe when * is the only wildcard.
export function glob(pattern) {
  const parts = pattern.split('*');
  if (parts.length === 1) {
    return (s) => s === pattern;
  }
  const head = parts[0],
    tail = parts[parts.length - 1],
    mid = parts.slice(1, -1);
  return (s) => {
    if (s.length < head.length + tail.length || !s.startsWith(head) || !s.endsWith(tail)) {
      return false;
    }
    const end = s.length - tail.length;
    let at = head.length;
    for (const p of mid) {
      const i = s.indexOf(p, at);
      if (i < 0 || i + p.length > end) {
        return false;
      }
      at = i + p.length;
    }
    return true;
  };
}

function typed(field, raw) {
  if (field.type === 'integer' || field.type === 'number') {
    const n = Number(raw);
    if (raw === '' || !Number.isFinite(n)) {
      return undefined;
    }
    return n;
  }
  if (field.type === 'boolean') {
    return raw === 'true' ? 1 : raw === 'false' ? 0 : undefined;
  }
  return raw;
}

// One filter as a function of a value giving true, false or null, as SQL's three-valued logic.
function test(field, value) {
  let v = value,
    not = false;
  if (v.startsWith('not.')) {
    not = true;
    v = v.slice(4);
  }
  const dot = v.indexOf('.');
  if (dot < 0) {
    return null;
  }
  const op = v.slice(0, dot),
    arg = v.slice(dot + 1);
  let fn,
    binds = 0;
  if (OPS.has(op)) {
    const t = typed(field, arg);
    if (t === undefined) {
      return null;
    }
    binds = 1;
    fn = (x) => {
      if (x === null) {
        return null;
      }
      const c = cmp(x, t);
      return op === 'eq'
        ? c === 0
        : op === 'neq'
          ? c !== 0
          : op === 'gt'
            ? c > 0
            : op === 'gte'
              ? c >= 0
              : op === 'lt'
                ? c < 0
                : c <= 0;
    };
  } else if (op === 'is' && arg === 'null') {
    fn = (x) => x === null;
  } else if (op === 'like' || op === 'ilike') {
    // SQLite's LIKE ignores case for ASCII letters only, which is also all LOWER() changes.
    if (field.type !== 'string') {
      return null;
    }
    binds = 1;
    const match = glob(lowerAscii(arg));
    fn = (x) => (x === null ? null : match(lowerAscii(String(x))));
  } else if (op === 'in') {
    const m = arg.match(/^\((.*)\)$/);
    if (!m) {
      return null;
    }
    const items = m[1]
      .split(',')
      .map((s) => s.trim())
      .filter((s) => s !== '')
      .map((s) => typed(field, s));
    if (!items.length || items.some((x) => x === undefined)) {
      return null;
    }
    binds = items.length;
    fn = (x) => (x === null ? null : items.some((t) => cmp(x, t) === 0));
  } else {
    return null;
  }
  return {
    binds,
    fn: not
      ? (x) => {
          const r = fn(x);
          return r === null ? null : !r;
        }
      : fn,
  };
}

// Neumaier's compensated sum, as SQLite adds floats, so the groups' totals lose no digits.
function add(a, x) {
  const t = a.sum + x;
  a.c += Math.abs(a.sum) >= Math.abs(x) ? a.sum - t + x : x - t + a.sum;
  a.sum = t;
}

function metricOf(spec, r) {
  const [fn, name] = spec.split('.');
  if (!FNS.has(fn)) {
    return null;
  }
  if (fn === 'count' && !name) {
    return { fn, as: 'count' };
  }
  if (!name || !Object.hasOwn(r.metrics, name)) {
    return null;
  }
  const f = r.fieldMap.get(name);
  if (!f) {
    return null;
  }
  if (
    fn !== 'count' &&
    fn !== 'min' &&
    fn !== 'max' &&
    !['integer', 'number', 'boolean'].includes(f.type)
  ) {
    return null;
  }
  return { fn, name, as: `${fn}_${name}` };
}

// The smallest cube holding every field the query names, with totals for its metric fields.
function cubeFor(r, params) {
  const need = new Set(
    (params.get('group') || '')
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean),
  );
  for (const k of params.keys()) {
    if (!RESERVED.has(k)) {
      need.add(k);
    }
  }
  const mets = (params.get('metric') || 'count')
    .split(',')
    .map((s) => s.split('.')[1])
    .filter(Boolean);
  let best = null;
  for (const c of r.cubes) {
    if (
      ![...need].every((f) => c.dims.includes(f)) ||
      !mets.every((m) => Object.hasOwn(c.metrics, m))
    ) {
      continue;
    }
    if (!best || c.count.length < best.count.length) {
      best = c;
    }
  }
  return best;
}

// The answer as /aggregate gives it, or null when the rollup cannot give that answer exactly.
export function aggregate(r, params) {
  if (!r.fieldMap) {
    r.fieldMap = new Map(r.fields.map((f) => [f.name, f]));
  }
  const c = cubeFor(r, params);
  return c ? aggregateCube(c, r.fieldMap, params) : null;
}

function aggregateCube(r, fieldMap, params) {
  r.fieldMap = fieldMap;
  const dimIx = new Map(r.dims.map((d, i) => [d, i]));
  const group = (params.get('group') || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
  if (group.some((g) => !dimIx.has(g))) {
    return null;
  }
  const specs = (params.get('metric') || 'count')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
  const metrics = specs.map((s) => metricOf(s, r));
  if (!metrics.length || metrics.some((m) => !m)) {
    return null;
  }
  const filters = [];
  let binds = 0;
  for (const [k, v] of params) {
    if (RESERVED.has(k)) {
      continue;
    }
    if (!dimIx.has(k)) {
      return null;
    }
    const t = test(r.fieldMap.get(k), v);
    if (!t) {
      return null;
    }
    binds += t.binds;
    // Each filter is decided once per distinct value, so a row costs a lookup.
    const d = dimIx.get(k);
    filters.push({ d, ok: r.values[d].map((x) => t.fn(x) === true) });
  }
  if (binds > MAX_PARAMS) {
    return null;
  }
  const limit = params.has('limit') ? Number(params.get('limit')) : LIMIT_DEFAULT;
  const offset = params.has('offset') ? Number(params.get('offset')) : 0;
  if (
    !Number.isInteger(limit) ||
    limit < 1 ||
    limit > LIMIT_MAX ||
    !Number.isInteger(offset) ||
    offset < 0
  ) {
    return null;
  }
  const allowed = new Set([...group, ...metrics.map((m) => m.as)]);
  const order = [];
  for (const p of (params.get('order') || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)) {
    const [name, dir = 'asc'] = p.split('.');
    if (!allowed.has(name) || (dir !== 'asc' && dir !== 'desc')) {
      return null;
    }
    order.push([name, dir === 'desc' ? -1 : 1]);
  }
  const value = (d, i) => r.values[d][r.codes[d][i]];
  const gix = group.map((g) => dimIx.get(g));
  // A group's key is its codes in mixed radix, a number, so no string is built per row.
  const radix = gix.map((d) => r.values[d].length);
  const buckets = new Map();
  let matched = 0;
  for (let i = 0; i < r.count.length; i++) {
    let pass = true;
    for (const f of filters) {
      if (!f.ok[r.codes[f.d][i]]) {
        pass = false;
        break;
      }
    }
    if (!pass) {
      continue;
    }
    matched += r.count[i];
    let k = 0;
    for (let j = 0; j < gix.length; j++) {
      k = k * radix[j] + r.codes[gix[j]][i];
    }
    let b = buckets.get(k);
    if (!b) {
      b = {
        keys: gix.map((d) => value(d, i)),
        count: 0,
        m: metrics.map(() => ({ sum: 0, c: 0, n: 0, min: null, max: null, any: false })),
      };
      buckets.set(k, b);
    }
    b.count += r.count[i];
    metrics.forEach((m, j) => {
      if (!m.name) {
        return;
      }
      const s = r.metrics[m.name],
        a = b.m[j];
      if (s.n[i] > 0) {
        a.any = true;
        add(a, s.sum[i]);
        a.n += s.n[i];
        if (a.min === null || cmp(s.min[i], a.min) < 0) {
          a.min = s.min[i];
        }
        if (a.max === null || cmp(s.max[i], a.max) > 0) {
          a.max = s.max[i];
        }
      }
    });
  }
  // Without a group SQL answers one row, even when nothing matched.
  if (!group.length && !buckets.size) {
    buckets.set(0, {
      keys: [],
      count: 0,
      m: metrics.map(() => ({ sum: 0, n: 0, min: null, max: null, any: false })),
    });
  }
  let rows = [...buckets.values()].map((b) => {
    const row = {};
    group.forEach((g, j) => {
      row[g] = b.keys[j];
    });
    metrics.forEach((m, j) => {
      const a = b.m[j];
      row[m.as] =
        m.fn === 'count'
          ? m.name
            ? a.n
            : b.count
          : m.fn === 'sum'
            ? a.any
              ? a.sum + a.c
              : null
            : m.fn === 'avg'
              ? a.any
                ? (a.sum + a.c) / a.n
                : null
              : m.fn === 'min'
                ? a.min
                : a.max;
    });
    return row;
  });
  // Ties keep group order, so a page boundary never moves between calls.
  const byGroup = (x, y) => {
    for (const g of group) {
      const c = cmp(x[g], y[g]);
      if (c) {
        return c;
      }
    }
    return 0;
  };
  rows.sort((x, y) => {
    for (const [n, d] of order) {
      const c = cmp(x[n], y[n]);
      if (c) {
        return c * d;
      }
    }
    return byGroup(x, y);
  });
  rows = rows.slice(offset, offset + limit + 1);
  const more = rows.length > limit;
  return { rows: more ? rows.slice(0, limit) : rows, more, matched };
}

const loaded = new Map();
let latest;

async function newest(ctx, slug) {
  if (!latest) {
    const r = await ctx.env.ASSETS.fetch(new Request(`${SITE}/latest.json`));
    if (!r.ok) {
      return null;
    }
    latest = await r.json();
  }
  return Object.hasOwn(latest, slug) ? latest[slug] : null;
}

// What names a published Parquet's bytes, as rollup.identity() in the pipeline stamps it.
export const identity = (o) =>
  o.customMetadata && o.customMetadata.sha256
    ? `sha256:${o.customMetadata.sha256}`
    : `etag:${o.etag}`;

// A rollup answers only while the Parquet it was built from is the one R2 publishes, so a
// rebuilt version never answers from its old rows. The check is repeated after VALID_MS.
const VALID_MS = 60_000;

const stampOf = (o) => (o && o.customMetadata && o.customMetadata.parquet) || null;

async function open(ctx, slug, version) {
  const k = key(slug, version);
  const hit = loaded.get(k);
  if (hit && Date.now() - hit.at < VALID_MS) {
    return hit.r;
  }
  loaded.delete(k);
  if (!ctx.env.DIST) {
    return null;
  }
  const pqKey = `d/${slug}/v/${version}/data.parquet`;
  // A rollup read before is checked by its metadata alone; a new one is read with its check.
  const [obj, pq] = await Promise.all([
    hit ? ctx.env.DIST.head(k) : ctx.env.DIST.get(k),
    ctx.env.DIST.head(pqKey),
  ]);
  const stamp = stampOf(obj);
  const fresh = stamp && pq && stamp === identity(pq);
  if (!fresh || (hit && hit.stamp === stamp)) {
    if (obj && obj.body) {
      await obj.body.cancel();
    }
    if (!fresh) {
      return null;
    }
    loaded.set(k, { ...hit, at: Date.now() });
    return hit.r;
  }
  const body = hit ? await ctx.env.DIST.get(k) : obj;
  if (!body || stampOf(body) !== stamp) {
    if (body && body.body) {
      await body.body.cancel();
    }
    return null;
  }
  const r = await gunzip(body.body).json();
  if (loaded.size >= KEEP) {
    loaded.delete(loaded.keys().next().value);
  }
  loaded.set(k, { r, stamp, at: Date.now() });
  return r;
}

// The same return shape as the other engines, or null to fall through. A withheld dataset is
// not in latest.json, so it falls through to the engine that answers 410. The deploy keeps a
// rollup only for a version D1 holds or the Parquet engine answers, and the caller cites each as
// that engine does.
export async function rollup(ctx, slug, version, op, qs) {
  if (op !== 'aggregate') {
    return null;
  }
  if (version && !VERSION.test(version)) {
    return null;
  }
  const live = await newest(ctx, slug);
  if (!live) {
    return null;
  }
  const v = version || live;
  const r = await open(ctx, slug, v);
  // As the Parquet engine refuses a file without provenance, its rollup does not answer either.
  if (!r || !r.publicdata || !Object.keys(r.publicdata).length) {
    return null;
  }
  const params = new URLSearchParams(qs.join('&'));
  const a = aggregate(r, params);
  if (!a) {
    return null;
  }
  const header = r.publicdata || {};
  return {
    version: v,
    rows: a.rows,
    more: a.more,
    matched: a.matched,
    query: `${API}/${slug}/versions/${v}/aggregate?${qs.join('&')}`,
    attribution: header.attribution,
    file: `${SITE}/d/${slug}/v/${v}/data.parquet`,
    manifest: `${SITE}/d/${slug}/v/${v}/manifest.json`,
  };
}
