import { parquetMetadata, parquetRead, parquetSchema } from 'hyparquet';
import { decompress } from 'fzstd';
import { fieldMap, filterSpecs, groupFields, likePattern, metricSpecs, orderSpecs, paging, selectFields } from './_query.js';

// The query API's rows and aggregate queries, answered from a version's data.parquet in R2 so
// that every version can be queried.

// What one call may read. Workers hide CPU time from the code that spends it, so cost is bounded
// by what decoding is proportional to: row groups, compressed bytes and the values decoded.
export const BUDGET = { groups: 128, bytes: 8 * 2 ** 20, values: 8_000_000, ranges: 160 };
const PARALLEL = 6;
// R2 answers a range in about 50 to 80 ms whatever its size, so near ranges are read as one.
const GAP = 256 * 1024;
const RUN = 8 * 2 ** 20;
const TAIL = 64 * 1024;
const FOOTER_MAX = 16 * 2 ** 20;
const FOOTERS = 32;

export class BudgetError extends Error {}

const compressors = { ZSTD: (input, n) => decompress(input, new Uint8Array(n)) };
// Dates and times as the query API gives them, as ISO text.
const iso = (ms) => new Date(ms).toISOString();
const parsers = {
  dateFromDays: (d) => (d == null ? null : iso(d * 86400000).slice(0, 10)),
  timestampFromMilliseconds: (t) => (t == null ? null : iso(Number(t)).slice(0, 19)),
  timestampFromMicroseconds: (t) => (t == null ? null : iso(Number(BigInt(t) / 1000n)).slice(0, 19)),
  timestampFromNanoseconds: (t) => (t == null ? null : iso(Number(BigInt(t) / 1000000n)).slice(0, 19)),
};

function fieldType(e) {
  const lt = e.logical_type && e.logical_type.type;
  if (e.type === 'BOOLEAN') return 'boolean';
  if (e.type === 'DOUBLE' || e.type === 'FLOAT') return 'number';
  if (e.converted_type === 'DATE' || lt === 'DATE') return 'date';
  if (lt === 'TIMESTAMP' || /^TIMESTAMP/.test(e.converted_type || '')) return 'datetime';
  if (e.type === 'INT32' || e.type === 'INT64') return 'integer';
  if (e.type === 'BYTE_ARRAY') return 'string';
  return null;
}

// Values as D1 returns them: booleans as 1 and 0, and a 64-bit integer as a number while it is
// exact, else as its digits.
function value(type, v) {
  if (v === undefined || v === null) return null;
  if (typeof v === 'bigint') return Number.isSafeInteger(Number(v)) ? Number(v) : String(v);
  if (type === 'boolean') return v ? 1 : 0;
  if (type === 'number' && Number.isNaN(v)) return null;
  return v;
}

function stat(type, s, rows) {
  if (!s) return null;
  const nulls = s.null_count === undefined || s.null_count === null ? undefined : Number(s.null_count);
  let min = s.is_min_value_exact === false ? undefined : s.min_value;
  let max = s.is_max_value_exact === false ? undefined : s.max_value;
  if (typeof min === 'bigint') min = Number.isSafeInteger(Number(min)) ? Number(min) : undefined;
  if (typeof max === 'bigint') max = Number.isSafeInteger(Number(max)) ? Number(max) : undefined;
  if (min instanceof Date || max instanceof Date) min = max = undefined;
  if (type === 'boolean') { min = min === undefined ? min : min ? 1 : 0; max = max === undefined ? max : max ? 1 : 0; }
  if (min === undefined || max === undefined || min === null || max === null) min = max = undefined;
  return { min, max, nulls, rows };
}

// UTF-8 byte order, which is how Parquet orders text statistics and SQLite orders text.
const unit = (c) => (c >= 0xe000 ? c - 0x800 : c >= 0xd800 ? c + 0x2000 : c);
function cmpText(a, b) {
  if (a === b) return 0;
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) {
    const x = a.charCodeAt(i), y = b.charCodeAt(i);
    if (x !== y) return unit(x) < unit(y) ? -1 : 1;
  }
  return a.length < b.length ? -1 : 1;
}
export function cmp(a, b) {
  if (typeof a === 'string' && typeof b === 'string') return cmpText(a, b);
  if (typeof a === 'string') a = Number(a);
  if (typeof b === 'string') b = Number(b);
  return a < b ? -1 : a > b ? 1 : 0;
}

const footers = new Map();

async function readRange(env, key, offset, length) {
  const o = await env.DIST.get(key, { range: { offset, length } });
  if (!o) throw new Error(`${key} is not in R2`);
  return new Uint8Array(await o.arrayBuffer());
}

async function readFooter(env, key) {
  const tail = await env.DIST.get(key, { range: { suffix: TAIL } });
  if (!tail) return null;
  const size = tail.size;
  let buf = new Uint8Array(await tail.arrayBuffer());
  const view = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
  if (buf.byteLength < 8 || view.getUint32(buf.byteLength - 4, true) !== 0x31524150) throw new Error(`${key} is not a Parquet file`);
  const len = view.getUint32(buf.byteLength - 8, true) + 8;
  if (len > FOOTER_MAX) throw new Error(`${key} has a footer of ${len} bytes`);
  if (len > buf.byteLength) buf = await readRange(env, key, size - len, len);
  const metadata = parquetMetadata(buf.slice(buf.byteLength - len).buffer, { parsers });
  const kv = (metadata.key_value_metadata || []).find((x) => x.key === 'publicdata');
  const header = kv ? JSON.parse(kv.value) : {};
  const fields = [];
  for (const node of parquetSchema(metadata).children) {
    const t = node.children.length ? null : fieldType(node.element);
    if (t) fields.push({ name: node.element.name, type: t });
  }
  // A nested column has no field, so only flat columns are mapped to their chunk.
  const types = new Map(fields.map((f) => [f.name, f.type]));
  let start = 0;
  const groups = metadata.row_groups.map((g) => {
    const rows = Number(g.num_rows);
    const chunks = {};
    g.columns.forEach((c) => {
      const md = c.meta_data;
      const name = md && md.path_in_schema.length === 1 ? md.path_in_schema[0] : null;
      if (!name || !types.has(name)) return;
      const at = Number(md.dictionary_page_offset || md.data_page_offset);
      chunks[name] = { start: at, end: at + Number(md.total_compressed_size), stats: stat(types.get(name), md.statistics, rows) };
    });
    const out = { start, rows, chunks };
    start += rows;
    return out;
  });
  return { key, size, metadata, header, fields, types, groups, rows: start };
}

// One version's footer, read once per isolate. A version never changes, so it is never stale.
export async function openVersion(env, slug, version) {
  const key = `d/${slug}/v/${version}/data.parquet`;
  if (!footers.has(key)) {
    const p = readFooter(env, key);
    footers.set(key, p);
    if (footers.size > FOOTERS) footers.delete(footers.keys().next().value);
    p.then((e) => { if (!e) footers.delete(key); }, () => footers.delete(key));
  }
  return footers.get(key);
}

const ascii = (v) => v.replace(/[A-Z]+/g, (c) => c.toLowerCase());

function prepare(specs) {
  return specs.map((s) => {
    const p = { ...s };
    if (s.op === 'in') p.set = new Set(s.args);
    // * is the only wildcard. SQLite's LIKE ignores case in ASCII letters only, and so does this.
    if (s.op === 'like' || s.op === 'ilike') p.re = new RegExp(`^${ascii(s.args[0]).split('*').map((x) => x.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&')).join('.*')}$`, 's');
    return p;
  });
}

function matches(s, v) {
  if (s.op === 'is') return s.not ? v !== null : v === null;
  if (v === null) return false;
  let r;
  const a = s.args[0];
  switch (s.op) {
    case 'eq': r = cmp(v, a) === 0; break;
    case 'neq': r = cmp(v, a) !== 0; break;
    case 'gt': r = cmp(v, a) > 0; break;
    case 'gte': r = cmp(v, a) >= 0; break;
    case 'lt': r = cmp(v, a) < 0; break;
    case 'lte': r = cmp(v, a) <= 0; break;
    case 'in': r = s.set.has(v) || s.args.some((x) => cmp(v, x) === 0); break;
    default: r = s.re.test(ascii(String(v)));
  }
  return s.not ? !r : r;
}

// Whether the statistics prove that no row of the group matches, or that every row does.
function none(s, st) {
  if (!st) return false;
  const allNull = st.nulls === st.rows;
  if (s.op === 'is') return s.not ? allNull : st.nulls === 0;
  if (allNull) return true;
  if (s.not || st.min === undefined) return false;
  const a = s.args[0], { min, max } = st;
  switch (s.op) {
    case 'eq': return cmp(a, min) < 0 || cmp(a, max) > 0;
    case 'in': return s.args.every((x) => cmp(x, min) < 0 || cmp(x, max) > 0);
    case 'neq': return cmp(min, a) === 0 && cmp(max, a) === 0;
    case 'gt': return cmp(max, a) <= 0;
    case 'gte': return cmp(max, a) < 0;
    case 'lt': return cmp(min, a) >= 0;
    case 'lte': return cmp(min, a) > 0;
    default: return false;
  }
}

function all(s, st) {
  if (!st || st.nulls === undefined) return false;
  if (s.op === 'is') return s.not ? st.nulls === 0 : st.nulls === st.rows;
  if (st.nulls !== 0 || s.not || st.min === undefined) return false;
  const a = s.args[0], { min, max } = st;
  switch (s.op) {
    case 'eq': return cmp(min, a) === 0 && cmp(max, a) === 0;
    case 'in': return cmp(min, max) === 0 && s.args.some((x) => cmp(x, min) === 0);
    case 'neq': return cmp(max, a) < 0 || cmp(min, a) > 0;
    case 'gt': return cmp(min, a) > 0;
    case 'gte': return cmp(min, a) >= 0;
    case 'lt': return cmp(max, a) < 0;
    case 'lte': return cmp(max, a) <= 0;
    default: return false;
  }
}

// The groups that can hold a match, each marked full when every row of it matches.
function prune(entry, specs) {
  const out = [];
  entry.groups.forEach((g, i) => {
    if (!g.rows) return;
    if (specs.some((s) => none(s, g.chunks[s.name].stats))) return;
    out.push({ i, g, full: specs.every((s) => all(s, g.chunks[s.name].stats)) });
  });
  return out;
}

function coalesce(chunks) {
  const sorted = chunks.map((c) => ({ start: c.start, end: c.end })).sort((a, b) => a.start - b.start);
  const out = [];
  for (const c of sorted) {
    const last = out[out.length - 1];
    if (last && c.start <= last.end + GAP && Math.max(last.end, c.end) - last.start <= RUN) last.end = Math.max(last.end, c.end);
    else out.push(c);
  }
  return out;
}

async function fetchAll(env, key, ranges) {
  let next = 0;
  const work = async () => {
    while (next < ranges.length) {
      const r = ranges[next++];
      r.buf = await readRange(env, key, r.start, r.end - r.start);
    }
  };
  await Promise.all(Array.from({ length: Math.min(PARALLEL, ranges.length) }, work));
  return ranges;
}

// An AsyncBuffer over the fetched ranges. hyparquet asks only for what was planned; anything else
// is read from R2 on its own.
function blockFile(env, entry, blocks) {
  return {
    byteLength: entry.size,
    slice(start, end = entry.size) {
      const b = blocks.find((x) => x.start <= start && end <= x.end);
      if (b) return b.buf.slice(start - b.start, end - b.start).buffer;
      return readRange(env, entry.key, start, end - start).then((u) => u.buffer);
    },
  };
}

class Scan {
  constructor(env, entry, budget, refuse) {
    Object.assign(this, { env, entry, budget, refuse });
    this.used = { groups: 0, bytes: 0, values: 0, ranges: 0 };
    this.cols = new Map();
    this.seen = new Set();
  }

  has(i, name, from, to) {
    const c = this.cols.get(i) && this.cols.get(i)[name];
    return !!c && c.from <= from && c.to >= to;
  }
  col(i, name) { return this.cols.get(i)[name].data; }

  // Reads the named columns of each group, rows from to to of it when given, after checking the
  // whole read against the budget. hyparquet skips the pages before a range and stops after it.
  async load(needs) {
    const todo = needs.map(({ i, names, from = 0, to = this.entry.groups[i].rows }) => (
      { i, from, to, names: [...new Set(names)].filter((n) => !this.has(i, n, from, to)) }
    )).filter((n) => n.names.length);
    if (!todo.length) return;
    const chunks = [];
    const u = { ...this.used };
    for (const { i, names, from, to } of todo) {
      const g = this.entry.groups[i];
      if (!this.seen.has(i)) u.groups++;
      for (const n of names) {
        chunks.push(g.chunks[n]);
        u.bytes += g.chunks[n].end - g.chunks[n].start;
        u.values += to - from;
      }
    }
    const ranges = coalesce(chunks);
    u.ranges += ranges.length;
    const over = Object.keys(this.budget).filter((k) => u[k] > this.budget[k]);
    if (over.length) throw this.refuse(u, over);
    this.used = u;
    const file = blockFile(this.env, this.entry, await fetchAll(this.env, this.entry.key, ranges));
    for (const { i, names, from, to } of todo) {
      this.seen.add(i);
      const g = this.entry.groups[i];
      const whole = from === 0 && to === g.rows;
      const got = {};
      for (const n of names) got[n] = { from, to, data: whole ? new Array(g.rows).fill(null) : new Array(g.rows) };
      await parquetRead({
        file, metadata: this.entry.metadata, columns: names, rowStart: g.start + from, rowEnd: g.start + to, compressors, parsers,
        onChunk: ({ columnName, columnData, rowStart }) => {
          const t = this.entry.types.get(columnName), out = got[columnName].data, at = rowStart - g.start;
          for (let j = 0; j < columnData.length; j++) out[at + j] = value(t, columnData[j]);
        },
      });
      this.cols.set(i, { ...(this.cols.get(i) || {}), ...got });
    }
  }

  // The rows of a group that match every filter, as indexes.
  hits(p, specs) {
    if (p.full || !specs.length) return null;
    const out = [];
    const cols = specs.map((s) => this.col(p.i, s.name));
    for (let r = 0; r < p.g.rows; r++) if (specs.every((s, k) => matches(s, cols[k][r]))) out.push(r);
    return out;
  }
}

const each = (hits, rows, fn) => { if (hits) for (const r of hits) fn(r); else for (let r = 0; r < rows; r++) fn(r); };

function sorter(order, get) {
  return (a, b) => {
    for (const { name, dir } of order) {
      const x = get(a, name), y = get(b, name);
      if (x === y) continue;
      // SQLite puts nulls first going up and last coming down.
      const c = x === null ? -1 : y === null ? 1 : cmp(x, y);
      if (c) return dir === 'asc' ? c : -c;
    }
    return 0;
  };
}

// SQL a caller can run with DuckDB against the version's Parquet file to get the same answer.
function lit(type, v) {
  if (type === 'boolean') return v ? 'true' : 'false';
  if (typeof v === 'number') return String(v);
  return `'${String(v).replace(/'/g, "''")}'`;
}
export function duckdbSQL(url, types, specs, tail) {
  const q = (n) => `"${n}"`;
  const where = specs.map(({ name, op, not, args }) => {
    const t = types.get(name);
    const ops = { eq: '=', neq: '!=', gt: '>', gte: '>=', lt: '<', lte: '<=' };
    let s;
    if (op in ops) s = `${q(name)} ${ops[op]} ${lit(t, args[0])}`;
    else if (op === 'is') s = `${q(name)} IS NULL`;
    else if (op === 'in') s = `${q(name)} IN (${args.map((a) => lit(t, a)).join(', ')})`;
    else s = `${q(name)} ILIKE ${lit('string', likePattern(args[0]))} ESCAPE '\\'`;
    return not ? `NOT (${s})` : s;
  });
  return `SELECT ${tail.select} FROM '${url}'${where.length ? ' WHERE ' + where.join(' AND ') : ''}${tail.rest}`;
}

function refusal(entry, url, sql, budget) {
  const mb = (n) => (n / 2 ** 20).toFixed(1);
  return (u, over) => {
    const what = { groups: `${u.groups} row groups`, bytes: `${mb(u.bytes)} MB`, values: `${u.values.toLocaleString('en-AU')} values`, ranges: `${u.ranges} reads` };
    const cap = { groups: `${budget.groups} row groups`, bytes: `${mb(budget.bytes)} MB`, values: `${budget.values.toLocaleString('en-AU')} values`, ranges: `${budget.ranges} reads` };
    return new BudgetError(
      `This query would read ${over.map((k) => what[k]).join(' and ')} of the version's ${entry.rows.toLocaleString('en-AU')} rows, more than one call may read (${over.map((k) => cap[k]).join(', ')}). `
      + `Narrow where to fewer rows, such as one year or one place. To answer it as asked, download ${url} or run this DuckDB SQL, which reads that dated version's Parquet file directly: ${sql}`,
    );
  };
}

// The rows of one version, as rowsQuery and answer() would give them from D1.
export async function parquetRows(env, entry, params, url, budget = BUDGET) {
  const m = fieldMap(entry.fields);
  const cols = selectFields(params, entry.fields, m);
  const specs = prepare(filterSpecs(params, m));
  const order = orderSpecs(params.get('order'), new Set(m.keys()));
  const { limit, offset } = paging(params);
  const q = (n) => `"${n}"`;
  const sql = duckdbSQL(url, entry.types, specs, {
    select: cols.map(q).join(', '),
    rest: `${order.length ? ' ORDER BY ' + order.map((o) => `${q(o.name)} ${o.dir.toUpperCase()}`).join(', ') : ''} LIMIT ${limit} OFFSET ${offset}`,
  });
  const scan = new Scan(env, entry, budget, refusal(entry, url, sql, budget));
  const groups = prune(entry, specs);
  const fnames = [...new Set(specs.map((s) => s.name))];
  await scan.load(groups.map((p) => ({ i: p.i, names: [...(p.full ? [] : fnames), ...order.map((o) => o.name)] })));
  const hits = groups.map((p) => scan.hits(p, specs));
  const matched = groups.reduce((n, p, k) => n + (hits[k] ? hits[k].length : p.g.rows), 0);
  let picks = [];
  if (order.length) {
    groups.forEach((p, k) => each(hits[k], p.g.rows, (r) => picks.push({ i: p.i, r })));
    picks.sort(sorter(order, (x, name) => scan.col(x.i, name)[x.r]));
    picks = picks.slice(offset, offset + limit + 1);
  } else {
    let skip = offset;
    for (let k = 0; k < groups.length && picks.length <= limit; k++) {
      const n = hits[k] ? hits[k].length : groups[k].g.rows;
      if (skip >= n) { skip -= n; continue; }
      const take = Math.min(n - skip, limit + 1 - picks.length);
      for (let j = skip; j < skip + take; j++) picks.push({ i: groups[k].i, r: hits[k] ? hits[k][j] : j });
      skip = 0;
    }
  }
  const more = picks.length > limit;
  picks = picks.slice(0, limit);
  const span = new Map();
  for (const p of picks) {
    const s = span.get(p.i);
    span.set(p.i, s ? { from: Math.min(s.from, p.r), to: Math.max(s.to, p.r + 1) } : { from: p.r, to: p.r + 1 });
  }
  await scan.load([...span].map(([i, s]) => ({ i, names: cols, ...s })));
  const rows = picks.map((p) => Object.fromEntries(cols.map((c) => [c, scan.col(p.i, c)[p.r]])));
  return { rows, matched, more, cols, used: scan.used, sql };
}

// Counts, sums, averages, minimums and maximums by group, as aggregateQuery gives them from D1.
export async function parquetAggregate(env, entry, params, url, budget = BUDGET) {
  const m = fieldMap(entry.fields);
  const group = groupFields(params, m);
  const metrics = metricSpecs(params, m);
  const specs = prepare(filterSpecs(params, m));
  const allowed = new Set([...group, ...metrics.map((x) => x.as)]);
  const order = orderSpecs(params.get('order'), allowed);
  const { limit, offset } = paging(params);
  const q = (n) => `"${n}"`;
  const sqlOrder = order.length ? order.map((o) => `${q(o.name)} ${o.dir.toUpperCase()}`) : group.map(q);
  const sql = duckdbSQL(url, entry.types, specs, {
    select: [...group.map(q), ...metrics.map((x) => `${x.fn.toUpperCase()}(${x.field ? q(x.field) : '*'}) AS ${q(x.as)}`)].join(', '),
    rest: `${group.length ? ' GROUP BY ' + group.map(q).join(', ') : ''}${sqlOrder.length ? ' ORDER BY ' + sqlOrder.join(', ') : ''} LIMIT ${limit} OFFSET ${offset}`,
  });
  const scan = new Scan(env, entry, budget, refusal(entry, url, sql, budget));
  const groups = prune(entry, specs);
  const fnames = [...new Set(specs.map((s) => s.name))];
  const mnames = metrics.filter((x) => x.field).map((x) => x.field);
  await scan.load(groups.map((p) => ({ i: p.i, names: [...(p.full ? [] : fnames), ...group, ...mnames] })));
  const buckets = new Map();
  let matched = 0;
  groups.forEach((p) => {
    const hits = scan.hits(p, specs);
    const gcols = group.map((n) => scan.col(p.i, n));
    const mcols = metrics.map((x) => (x.field ? scan.col(p.i, x.field) : null));
    each(hits, p.g.rows, (r) => {
      matched++;
      const vals = gcols.map((c) => c[r]);
      const key = JSON.stringify(vals);
      let b = buckets.get(key);
      if (!b) buckets.set(key, (b = { vals, acc: metrics.map(() => ({ n: 0, sum: 0, best: null })) }));
      metrics.forEach((x, k) => {
        const a = b.acc[k];
        if (!x.field) { a.n++; return; }
        const v = mcols[k][r];
        if (v === null) return;
        a.n++;
        if (x.fn === 'sum' || x.fn === 'avg') a.sum += Number(v);
        else if (x.fn === 'min' && (a.best === null || cmp(v, a.best) < 0)) a.best = v;
        else if (x.fn === 'max' && (a.best === null || cmp(v, a.best) > 0)) a.best = v;
      });
    });
  });
  // With no group there is one answer row, even when nothing matches, as in SQL.
  if (!group.length && !buckets.size) buckets.set('[]', { vals: [], acc: metrics.map(() => ({ n: 0, sum: 0, best: null })) });
  let out = [...buckets.values()].map((b) => {
    const row = {};
    group.forEach((n, k) => { row[n] = b.vals[k]; });
    metrics.forEach((x, k) => {
      const a = b.acc[k];
      row[x.as] = x.fn === 'count' ? a.n : x.fn === 'sum' ? (a.n ? a.sum : null) : x.fn === 'avg' ? (a.n ? a.sum / a.n : null) : a.best;
    });
    return row;
  });
  const byGroup = group.map((name) => ({ name, dir: 'asc' }));
  out.sort(sorter([...order, ...byGroup], (row, name) => row[name]));
  const more = out.length > offset + limit;
  out = out.slice(offset, offset + limit);
  return { rows: out, matched, more, cols: [...group, ...metrics.map((x) => x.as)], used: scan.used, sql };
}

