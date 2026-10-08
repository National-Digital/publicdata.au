import { parquetMetadata, parquetRead, parquetSchema, readColumnIndex, readOffsetIndex } from 'hyparquet';
import { decompress } from 'fzstd';
import { fieldMap, filterSpecs, groupFields, likePattern, metricSpecs, orderSpecs, paging, selectFields } from './_query.js';

// The query API's rows and aggregate queries, answered from Parquet in R2 for the versions D1 does
// not hold. Only a file written under the query profile is read, as its footer key says
// (pipeline/publicdata/serialise/profile.py). A sorted one has a page index, so a
// filter on the sort reads few pages; without one, a column chunk is read as a single page.

// What one call may read. Workers hide CPU time from the code that spends it, so cost is bounded
// by what decoding is proportional to: row groups, compressed bytes and the values decoded. An
// ordered page keeps every row before it in a heap, so offset + limit is held to its own cap, and
// an aggregate holds one bucket per distinct group, so those are capped too.
export const BUDGET = { groups: 64, bytes: 8 * 2 ** 20, values: 4_000_000, ranges: 160, held: 100_000, buckets: 50_000 };
const PARALLEL = 6;
const STREAM = 8;
// R2 answers a range in about 50 to 80 ms whatever its size, so near ranges are read as one.
const GAP = 256 * 1024;
const RUN = 8 * 2 ** 20;
const TAIL = 64 * 1024;
const FOOTER_MAX = 16 * 2 ** 20;
const FOOTERS = 32;
const PROFILE = 'publicdata.profile';
const PROFILES = new Set(['1']);

// Part of every cached answer's key, raised when a change alters what an answer holds.
export const ENGINE = '2';

export class BudgetError extends Error {}
// A range read refused because the object is no longer the one its footer came from.
export class StaleError extends Error {}

// A query copy is written again in place when its entry's sort, lookup or int32 changes, so a
// footer is held to the ETag it was read under. Past this age the copy's ETag is checked before
// the footer, and any answer cached under its ETag, is trusted again.
export const RECHECK = { ms: 60_000 };

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

// The suppressed flags are a list of field names, which D1 holds joined by semicolons.
function listType(node) {
  const lt = node.element.logical_type && node.element.logical_type.type;
  if (node.element.converted_type !== 'LIST' && lt !== 'LIST') return null;
  let n = node;
  while (n.children.length === 1) n = n.children[0];
  return !n.children.length && n.element.type === 'BYTE_ARRAY' ? 'array' : null;
}

// Values as D1 returns them: booleans as 1 and 0, a list joined by semicolons, and a 64-bit
// integer as a number while it is exact, else as its digits.
function value(type, v) {
  if (v === undefined || v === null) return null;
  if (type === 'array') return Array.isArray(v) && v.length ? v.join(';') : null;
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

async function readRange(env, key, offset, length, etag) {
  const o = await env.DIST.get(key, { range: { offset, length }, onlyIf: { etagMatches: etag } });
  if (!o) throw new Error(`${key} is not in R2`);
  // R2 answers a failed precondition with the object's metadata and no body.
  if (typeof o.arrayBuffer !== 'function') throw new StaleError(`${key} changed since its footer was read`);
  const buf = new Uint8Array(await o.arrayBuffer());
  if (buf.byteLength !== length) throw new Error(`${key} gave ${buf.byteLength} bytes at ${offset}, not ${length}`);
  return buf;
}

async function readFooter(env, key) {
  const tail = await env.DIST.get(key, { range: { suffix: TAIL } });
  if (!tail) return null;
  const size = tail.size, etag = tail.etag;
  let buf = new Uint8Array(await tail.arrayBuffer());
  const view = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
  if (buf.byteLength < 8 || view.getUint32(buf.byteLength - 4, true) !== 0x31524150) throw new Error(`${key} is not a Parquet file`);
  const len = view.getUint32(buf.byteLength - 8, true) + 8;
  if (len > FOOTER_MAX) throw new Error(`${key} has a footer of ${len} bytes`);
  if (len > buf.byteLength) buf = await readRange(env, key, size - len, len, etag);
  const metadata = parquetMetadata(buf.slice(buf.byteLength - len).buffer, { parsers });
  const kv = (metadata.key_value_metadata || []).find((x) => x.key === 'publicdata');
  const header = kv ? JSON.parse(kv.value) : null;
  const fields = [];
  const elements = new Map();
  for (const node of parquetSchema(metadata).children) {
    const t = node.children.length ? listType(node) : fieldType(node.element);
    if (t) { fields.push({ name: node.element.name, type: t }); elements.set(node.element.name, node.element); }
  }
  const types = new Map(fields.map((f) => [f.name, f.type]));
  let start = 0;
  const groups = metadata.row_groups.map((g) => {
    const rows = Number(g.num_rows);
    const chunks = {};
    for (const c of g.columns) {
      const md = c.meta_data;
      const name = md && md.path_in_schema[0];
      if (!name || !types.has(name) || (md.path_in_schema.length > 1) !== (types.get(name) === 'array')) continue;
      const at = Number(md.dictionary_page_offset || md.data_page_offset);
      const span = (o, l) => (o !== undefined && o !== null && l ? { start: Number(o), end: Number(o) + l } : null);
      const ci = span(c.column_index_offset, c.column_index_length), oi = span(c.offset_index_offset, c.offset_index_length);
      // A list's statistics describe its items, so they say nothing about the rows.
      const list = types.get(name) === 'array';
      chunks[name] = { start: at, end: at + Number(md.total_compressed_size), stats: list ? null : stat(types.get(name), md.statistics, rows), ci: list ? null : ci, oi: list ? null : oi, list };
    }
    const out = { start, rows, chunks };
    start += rows;
    return out;
  });
  const mark = (metadata.key_value_metadata || []).find((x) => x.key === PROFILE);
  const profiled = !!mark && PROFILES.has(mark.value);
  // The order a profile file is written in: its sort, then the key, then the source position.
  const leaves = metadata.row_groups.length ? metadata.row_groups[0].columns : [];
  const sortedBy = ((metadata.row_groups[0] && metadata.row_groups[0].sorting_columns) || [])
    .map((c) => ({ name: leaves[c.column_idx].meta_data.path_in_schema[0], desc: !!c.descending, nullsFirst: !!c.nulls_first }));
  return { key, size, etag, metadata, header, fields, types, elements, groups, rows: start, profiled, sortedBy, pages: new Map() };
}

// The build writes a profile copy of every version, old ones included, at _q/, which no public
// route serves. The published file answers only when it carries the profile itself.
async function locate(env, slug, version) {
  const [q, pub] = await Promise.all([
    readFooter(env, `_q/${slug}/${version}.parquet`).catch((e) => {
      // A copy that is not a Parquet file is passed over; a failed read is not, or the isolate
      // would keep the published file in its place.
      if (/is not a Parquet file|has a footer of/.test(e.message)) { console.error(`_q ${slug} ${version}: ${e.message}`); return null; }
      throw e;
    }),
    readFooter(env, `d/${slug}/v/${version}/data.parquet`),
  ]);
  // The copy's ETag is kept either way, so a copy written again later is noticed.
  const copy = q ? q.etag : null;
  if (q && q.profiled && q.header && pub && sameVersion(q, pub)) return Object.assign(q, { copy });
  if (q && pub) console.error(`_q ${slug} ${version}: the copy does not match the published file`);
  return pub && Object.assign(pub, { copy });
}

// Answers name the published file, so a copy answers only when it holds the same version of the
// same source in the same number of rows.
function sameVersion(q, pub) {
  if (q.rows !== pub.rows || !pub.header) return false;
  const a = q.header, b = pub.header;
  return a.dataset === b.dataset && a.version === b.version
    && JSON.stringify(a.source && a.source.sha256) === JSON.stringify(b.source && b.source.sha256);
}

// Whether the query copy in R2 is no longer the one a footer was located with.
async function moved(env, slug, version, p) {
  try {
    const [e, now] = await Promise.all([p, env.DIST.head(`_q/${slug}/${version}.parquet`)]);
    return !e || (now ? now.etag : null) !== e.copy;
  } catch {
    return true;
  }
}

// One version's footer, read once per isolate. The published file never changes, but its query
// copy can be written again, so every range read is held to the footer's ETag (StaleError) and an
// old footer is checked against the copy's ETag.
export async function openVersion(env, slug, version) {
  const id = `${slug}/${version}`;
  let hit = footers.get(id);
  // Least recently used goes first: a hit moves to the back of the map's order.
  if (hit) {
    footers.delete(id);
    if (Date.now() - hit.at >= RECHECK.ms) {
      hit.at = Date.now();
      if (await moved(env, slug, version, hit.p)) hit = null;
    }
  }
  if (!hit) {
    const p = locate(env, slug, version);
    hit = { p, at: Date.now() };
    const drop = () => { if (footers.get(id) === hit) footers.delete(id); };
    p.then((e) => { if (!e) drop(); }, drop);
  }
  footers.set(id, hit);
  if (footers.size > FOOTERS) footers.delete(footers.keys().next().value);
  return hit.p;
}

// Drops a version's footer, after a read found its file changed.
export function forget(slug, version) {
  footers.delete(`${slug}/${version}`);
}

function coalesce(chunks) {
  const sorted = chunks.filter((c) => c.end > c.start).map((c) => ({ start: c.start, end: c.end })).sort((a, b) => a.start - b.start);
  const out = [];
  for (const c of sorted) {
    const last = out[out.length - 1];
    if (last && c.start <= last.end + GAP && Math.max(last.end, c.end) - last.start <= RUN) last.end = Math.max(last.end, c.end);
    else out.push(c);
  }
  return out;
}

async function fetchAll(env, entry, ranges) {
  let next = 0;
  const work = async () => {
    while (next < ranges.length) {
      const r = ranges[next++];
      r.buf = await readRange(env, entry.key, r.start, r.end - r.start, entry.etag);
    }
  };
  await Promise.all(Array.from({ length: Math.min(PARALLEL, ranges.length) }, work));
  return ranges;
}

// hyparquet converts a date or time bound as it reads it and throws on the empty bound of a page
// that is all null, so those are read as plain integers and converted here.
function columnIndex(buf, element, type) {
  const raw = type === 'date' || type === 'datetime';
  const ix = readColumnIndex({ view: new DataView(buf.buffer), offset: 0 }, raw ? { type: element.type } : element, parsers);
  if (!raw) return ix;
  const unit = (element.logical_type && element.logical_type.unit) || (element.converted_type === 'TIMESTAMP_MICROS' ? 'MICROS' : 'MILLIS');
  const conv = (v) => {
    if (typeof v !== 'number' && typeof v !== 'bigint') return undefined;
    if (type === 'date') return parsers.dateFromDays(Number(v));
    return unit === 'NANOS' ? parsers.timestampFromNanoseconds(v) : unit === 'MICROS' ? parsers.timestampFromMicroseconds(v) : parsers.timestampFromMilliseconds(v);
  };
  return { ...ix, min_values: ix.min_values.map(conv), max_values: ix.max_values.map(conv) };
}

// The page index of the named columns in every row group, read once per version and column.
// Each page carries its rows, its bytes and its statistics in the form the predicates compare.
async function pageIndex(env, entry, names) {
  const todo = [...new Set(names)].filter((n) => !entry.pages.has(n));
  if (todo.length) {
    const want = [];
    for (const n of todo) for (const g of entry.groups) { const c = g.chunks[n]; if (c.oi) want.push(c.oi, ...(c.ci ? [c.ci] : [])); }
    const read = fetchAll(env, entry, coalesce(want));
    for (const n of todo) {
      entry.pages.set(n, read.then((blocks) => entry.groups.map((g) => {
        const c = g.chunks[n];
        // An unsorted profile file has no page index, so the chunk is one page with its statistics.
        if (!c.oi) return { pages: [{ from: 0, to: g.rows, start: c.start, end: c.end, st: c.stats }], dict: { start: c.start, end: c.start }, oi: null };
        const bytes = (r) => { const b = blocks.find((x) => x.start <= r.start && r.end <= x.end); return b.buf.slice(r.start - b.start, r.end - b.start); };
        const oiBytes = bytes(c.oi);
        const locs = readOffsetIndex({ view: new DataView(oiBytes.buffer), offset: 0 }).page_locations;
        const t = entry.types.get(n);
        const ix = c.ci ? columnIndex(bytes(c.ci), entry.elements.get(n), t) : null;
        const pages = locs.map((l, k) => {
          const from = Number(l.first_row_index), to = k + 1 < locs.length ? Number(locs[k + 1].first_row_index) : g.rows;
          const st = ix ? stat(t, {
            min_value: ix.null_pages[k] ? undefined : ix.min_values[k],
            max_value: ix.null_pages[k] ? undefined : ix.max_values[k],
            null_count: ix.null_pages[k] ? to - from : ix.null_counts ? ix.null_counts[k] : undefined,
          }, to - from) : null;
          return { from, to, start: Number(l.offset), end: Number(l.offset) + l.compressed_page_size, st };
        });
        // hyparquet reads the dictionary and the offset index beside the pages it decodes.
        return { pages, dict: { start: c.start, end: pages.length ? pages[0].start : c.start }, oi: { ...c.oi, buf: oiBytes } };
      })));
      entry.pages.get(n).catch(() => entry.pages.delete(n));
    }
  }
  return Object.fromEntries(await Promise.all([...new Set(names)].map(async (n) => [n, await entry.pages.get(n)])));
}

// The row ranges of a group a query must look at: none where a page proves no row matches, and
// marked all where its pages prove every row does, so those are counted without being read.
function segments(g, gi, specs, ix) {
  if (!specs.length) return [{ from: 0, to: g.rows, all: true }];
  const cuts = new Set([0, g.rows]);
  for (const s of specs) for (const p of ix[s.name][gi].pages) cuts.add(p.from);
  const edges = [...cuts].sort((a, b) => a - b);
  const out = [];
  for (let k = 0; k + 1 < edges.length; k++) {
    const from = edges[k], to = edges[k + 1];
    let state = 2;
    for (const s of specs) {
      const p = ix[s.name][gi].pages.find((x) => x.from <= from && from < x.to);
      state = Math.min(state, none(s, p.st) ? 0 : all(s, p.st) ? 2 : 1);
    }
    if (!state) continue;
    const last = out[out.length - 1];
    if (last && last.to === from && last.all === (state === 2)) last.to = to;
    else out.push({ from, to, all: state === 2 });
  }
  return out;
}

// The groups that can hold a match, with the row ranges in each that can.
function prune(entry, specs, ix) {
  const out = [];
  entry.groups.forEach((g, i) => {
    if (!g.rows || specs.some((s) => none(s, g.chunks[s.name].stats))) return;
    const segs = segments(g, i, specs, ix);
    if (segs.length) out.push({ i, g, segs });
  });
  return out;
}

// An AsyncBuffer over the fetched ranges. A slice across two of them is joined; anything not
// fetched is read from R2 on its own.
function blockFile(env, entry, blocks) {
  return {
    byteLength: entry.size,
    slice(start, end = entry.size) {
      const one = blocks.find((x) => x.start <= start && end <= x.end);
      if (one) return one.buf.slice(start - one.start, end - one.start).buffer;
      const parts = blocks.filter((x) => x.end > start && x.start < end).sort((a, b) => a.start - b.start);
      let at = start;
      for (const b of parts) { if (b.start > at) break; at = Math.max(at, b.end); }
      if (at >= end) {
        const out = new Uint8Array(end - start);
        for (const b of parts) {
          const from = Math.max(b.start, start), to = Math.min(b.end, end);
          out.set(b.buf.subarray(from - b.start, to - b.start), from - start);
        }
        return out.buffer;
      }
      return readRange(env, entry.key, start, end - start, entry.etag).then((u) => u.buffer);
    },
  };
}

class Scan {
  constructor(env, entry, ix, budget, refuse, specs = []) {
    Object.assign(this, { env, entry, ix, budget, refuse });
    this.used = { groups: 0, bytes: 0, values: 0, ranges: 0, held: 0, buckets: 0 };
    this.weight = likeWeights(specs);
    this.cols = new Map();
    this.seen = new Set();
  }

  has(i, name, from, to) {
    const c = this.cols.get(i) && this.cols.get(i)[name];
    return !!c && c.spans.some(([a, b]) => a <= from && to <= b);
  }
  col(i, name) { return this.cols.get(i)[name].data; }

  // What a read of rows from to to of the named columns of each group will fetch and decode,
  // worked out from the page index before any data is read.
  plan(needs) {
    const todo = [];
    for (const { i, names, from, to } of needs) {
      const g = this.entry.groups[i];
      const uniq = [...new Set(names)];
      // hyparquet decodes a list column a whole chunk at a time, so its rows are read whole.
      const parts = [[uniq.filter((n) => !g.chunks[n].list), from, to], [uniq.filter((n) => g.chunks[n].list), 0, g.rows]];
      for (const [ns, a, b] of parts) {
        const left = ns.filter((n) => !this.has(i, n, a, b));
        if (left.length && b > a) todo.push({ i, from: a, to: b, names: left });
      }
    }
    const want = [];
    const cost = { groups: new Set(todo.map((t) => t.i)), bytes: 0, values: 0 };
    for (const { i, names, from, to } of todo) {
      for (const n of names) {
        const { pages, dict, oi } = this.ix[n][i];
        const hit = pages.filter((p) => p.to > from && p.from < to);
        want.push(dict, { start: hit[0].start, end: hit[hit.length - 1].end }, ...(oi ? [oi] : []));
        cost.bytes += dict.end - dict.start + hit.reduce((a, p) => a + p.end - p.start, 0);
        cost.values += hit.reduce((a, p) => a + p.to - p.from, 0) * (this.weight.get(n) || 1);
      }
    }
    return { todo, want, ranges: coalesce(want.filter((r) => !r.buf)), cost };
  }

  // Holds a read to the budget, refusing the query before anything is fetched.
  charge(needs, extra = {}) {
    const { todo, ranges, cost } = this.plan(needs);
    const u = { ...this.used };
    for (const k of Object.keys(extra)) u[k] += extra[k];
    for (const i of cost.groups) if (!this.seen.has(i)) { u.groups++; this.seen.add(i); }
    u.bytes += cost.bytes;
    u.values += cost.values;
    u.ranges += ranges.length;
    const over = Object.keys(this.budget).filter((k) => u[k] > this.budget[k]);
    if (over.length) throw this.refuse(u, over);
    this.used = u;
    return todo;
  }

  async read(todo) {
    const { want, ranges } = this.plan(todo);
    if (!todo.length) return;
    const blocks = [...want.filter((r) => r.buf), ...(await fetchAll(this.env, this.entry, ranges))];
    const file = blockFile(this.env, this.entry, blocks);
    for (const { i, names, from, to } of todo) {
      const g = this.entry.groups[i];
      const have = this.cols.get(i) || {};
      for (const n of names) if (!have[n]) have[n] = { spans: [], data: null };
      this.cols.set(i, have);
      await parquetRead({
        file, metadata: this.entry.metadata, columns: names, rowStart: g.start + from, rowEnd: g.start + to, useOffsetIndex: true, compressors, parsers,
        onChunk: ({ columnName, columnData, rowStart }) => {
          const t = this.entry.types.get(columnName), c = have[columnName], at = rowStart - g.start;
          // A whole group in one plain array is converted where it lies, which spares a copy.
          if (!c.data && at === 0 && columnData.length === g.rows && Array.isArray(columnData)) {
            for (let j = 0; j < columnData.length; j++) columnData[j] = value(t, columnData[j]);
            c.data = columnData;
            return;
          }
          if (!c.data) c.data = new Array(g.rows);
          for (let j = 0; j < columnData.length; j++) c.data[at + j] = value(t, columnData[j]);
        },
      });
      for (const n of names) have[n].spans.push([from, to]);
    }
  }

  load(needs) { return this.read(this.charge(needs)); }

  // Reads the charged needs a few groups at a time, handing each group to fn and then letting its
  // columns go, so a whole-table count never holds every column in memory at once.
  async stream(groups, todo, fn) {
    for (let k = 0; k < groups.length; k += STREAM) {
      const batch = groups.slice(k, k + STREAM);
      const ids = new Set(batch.map((p) => p.i));
      await this.read(todo.filter((t) => ids.has(t.i)));
      for (const p of batch) { fn(p); this.cols.delete(p.i); }
    }
  }

  // The rows of a segment that match every filter, as indexes, or null when all of them do.
  hits(p, seg, specs) {
    if (seg.all) return null;
    const out = [];
    const cols = specs.map((s) => this.col(p.i, s.name));
    for (let r = seg.from; r < seg.to; r++) if (specs.every((s, k) => matches(s, cols[k][r]))) out.push(r);
    return out;
  }
}

// Each matching row of a group, in order, without building a row object.
function eachHit(scan, p, specs, fn) {
  for (const seg of p.segs) {
    const h = scan.hits(p, seg, specs);
    if (h) for (const r of h) fn(r);
    else for (let r = seg.from; r < seg.to; r++) fn(r);
  }
}

// A group's matches as segments, each a range the statistics proved or the list of rows that
// passed, so a proven range is counted and paged by arithmetic and never held row by row.
function matchesOf(scan, p, specs) {
  return p.segs.map((seg) => {
    const h = scan.hits(p, seg, specs);
    return h ? { rows: h, n: h.length } : { from: seg.from, n: seg.to - seg.from };
  });
}
const nth = (m, j) => (m.rows ? m.rows[j] : m.from + j);

// Compensated (Kahan-Babuska-Neumaier) summation, as SQLite sums, so ten 0.1s make 1.
export function addInto(a, n) {
  const t = a.sum + n;
  a.c += Math.abs(a.sum) >= Math.abs(n) ? (a.sum - t) + n : (n - t) + a.sum;
  a.sum = t;
}

// A like pattern with a * can cost its length times the value's length to match, so each value
// a like filter reads is charged as one value for every eight characters of pattern.
function likeWeights(specs) {
  const w = new Map();
  for (const s of specs) {
    if ((s.op === 'like' || s.op === 'ilike') && s.args[0].includes('*')) {
      w.set(s.name, (w.get(s.name) || 1) + Math.ceil(s.args[0].length / 8));
    }
  }
  return w;
}

const needsOf = (groups, names, filterNames) => groups.flatMap((p) => p.segs.map((seg) => (
  { i: p.i, from: seg.from, to: seg.to, names: [...(seg.all ? [] : filterNames), ...names] }
)));

const ascii = (v) => v.replace(/[A-Z]+/g, (c) => c.toLowerCase());

// A pattern with * as its only wildcard, matched without backtracking: on a mismatch the last *
// takes one more character, so the work is at most the pattern's length times the value's.
export function glob(p, v) {
  let i = 0, j = 0, star = -1, mark = 0;
  while (j < v.length) {
    if (i < p.length && p[i] === '*') { star = i++; mark = j; }
    else if (i < p.length && p[i] === v[j]) { i++; j++; }
    else if (star >= 0) { i = star + 1; j = ++mark; }
    else return false;
  }
  while (i < p.length && p[i] === '*') i++;
  return i === p.length;
}

export function prepare(specs) {
  return specs.map((s) => {
    const p = { ...s };
    if (s.op === 'in') p.set = new Set(s.args);
    // * is the only wildcard. SQLite's LIKE ignores case in ASCII letters only, and so does this.
    if (s.op === 'like' || s.op === 'ilike') p.glob = ascii(s.args[0]);
    return p;
  });
}

export function matches(s, v) {
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
    default: r = glob(s.glob, ascii(String(v)));
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
// The order a profile file holds its rows in, which the DuckDB SQL reproduces from the
// published file: the sort, then the key, then the source position.
function fileOrder(entry) {
  return [...entry.sortedBy.map((c) => `"${c.name}" ${c.desc ? 'DESC' : 'ASC'} NULLS ${c.nullsFirst ? 'FIRST' : 'LAST'}`), 'file_row_number'];
}
// SQLite puts nulls first going up and last coming down, so DuckDB is told the same.
const sqlOrder = (o) => `"${o.name}" ${o.dir === 'asc' ? 'ASC NULLS FIRST' : 'DESC NULLS LAST'}`;
const col = (types, n) => (types.get(n) === 'array' ? `NULLIF(array_to_string("${n}", ';'), '')` : `"${n}"`);

export function duckdbSQL(url, types, specs, tail) {
  const q = (n) => col(types, n);
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
  return `SELECT ${tail.select} FROM read_parquet('${url}', file_row_number = true)${where.length ? ' WHERE ' + where.join(' AND ') : ''}${tail.rest}`;
}

// Advice for a page past the held cap that loses no rows: restart from the boundary value with an
// inclusive bound, since a strict one skips the rest of a value that has ties.
function deeper(order, cap) {
  const o = order[0];
  const op = o && o.dir === 'asc' ? 'gte' : 'lte';
  return `An ordered page holds every row before it, so keep offset + limit under ${cap}. `
    + (o ? `To page further, filter ${o.name}=${op}.<the last ${o.name} a page gave>, start offset again at 0 and skip the rows of that value you already have, or order by a field that is unique in this version.` : '');
}

function refusal(entry, url, sql, budget, order = []) {
  const mb = (n) => (n / 2 ** 20).toFixed(1);
  return (u, over) => {
    const n = (x) => x.toLocaleString('en-AU');
    const what = { groups: `${u.groups} row groups`, bytes: `${mb(u.bytes)} MB`, values: `${n(u.values)} values`, ranges: `${u.ranges} reads`, held: `${n(u.held)} ordered rows`, buckets: `${n(u.buckets)} distinct groups` };
    const cap = { groups: `${budget.groups} row groups`, bytes: `${mb(budget.bytes)} MB`, values: `${n(budget.values)} values`, ranges: `${budget.ranges} reads`, held: `${n(budget.held)} ordered rows`, buckets: `${n(budget.buckets)} distinct groups` };
    const narrow = over.includes('held')
      ? deeper(order, n(budget.held))
      : over.includes('buckets')
        ? 'Group by fewer or coarser fields, or narrow where to fewer rows.'
        : 'Narrow where to fewer rows, such as one year or one place.';
    return new BudgetError(
      `This query would read ${over.map((k) => what[k]).join(' and ')} of the version's ${n(entry.rows)} rows, more than one call may read (${over.map((k) => cap[k]).join(', ')}). `
      + `${narrow} To answer it as asked, download ${url} or run this DuckDB SQL, which reads that dated version's Parquet file directly: ${sql}`,
    );
  };
}


// A file written before the query profile is not scanned: unsorted, with small row groups and no
// page index, it costs seconds of CPU.
function unprofiled(entry, url, sql) {
  return new BudgetError(
    `This version's Parquet file was written before the query profile and has not been rebuilt yet, so this server does not scan it. `
    + `Until it has been, download ${url} or run this DuckDB SQL, which reads that file directly: ${sql}`,
  );
}

// The rows of one version, as rowsQuery and answer() would give them from D1.
export async function parquetRows(env, entry, params, url, budget = BUDGET) {
  const m = fieldMap(entry.fields);
  const cols = selectFields(params, entry.fields, m);
  const specs = prepare(filterSpecs(params, m));
  const order = orderSpecs(params.get('order'), new Set(m.keys()));
  const { limit, offset } = paging(params);
  const sql = duckdbSQL(url, entry.types, specs, {
    select: cols.map((c) => `${col(entry.types, c)} AS "${c}"`).join(', '),
    rest: ` ORDER BY ${[...order.map(sqlOrder), ...fileOrder(entry)].join(', ')} LIMIT ${limit} OFFSET ${offset}`,
  });
  if (!entry.profiled) throw unprofiled(entry, url, sql);
  const fnames = [...new Set(specs.map((s) => s.name))];
  const onames = order.map((o) => o.name);
  const ix = await pageIndex(env, entry, [...fnames, ...onames, ...cols]);
  const scan = new Scan(env, entry, ix, budget, refusal(entry, url, sql, budget, order), specs);
  const groups = prune(entry, specs, ix);
  const want = offset + limit + 1;
  // The heap never holds more rows than can match, and each candidate costs a compare per level
  // of it: one more value for every eight levels, measured against decoding.
  const candidates = groups.reduce((n, p) => n + p.segs.reduce((a, s) => a + s.to - s.from, 0), 0);
  const held = order.length ? Math.min(want, candidates) : 0;
  const depth = held > 1 ? Math.floor(Math.log2(held) / 8) : 0;
  const todo = scan.charge(needsOf(groups, onames, fnames), { held, values: candidates * depth });
  const found = [];
  const collect = (p) => found.push(matchesOf(scan, p, specs));
  // An order needs its columns for every match, so those are kept; filter columns are not.
  if (order.length) { await scan.read(todo); groups.forEach(collect); } else await scan.stream(groups, todo, collect);
  const matched = found.reduce((n, ms) => n + ms.reduce((a, x) => a + x.n, 0), 0);
  let picks = [];
  if (order.length) {
    // The first offset + limit + 1 matches in order, kept in a heap of that size. Ties fall to
    // the file's own order.
    const by = sorter(order, (x, name) => scan.col(x.i, name)[x.r]);
    const less = (x, y) => by(x, y) || x.i - y.i || x.r - y.r;
    const heap = [];
    const up = (k) => { while (k) { const h = (k - 1) >> 1; if (less(heap[h], heap[k]) >= 0) break; [heap[h], heap[k]] = [heap[k], heap[h]]; k = h; } };
    const down = (k) => {
      for (;;) {
        const l = 2 * k + 1, r = l + 1;
        let top = k;
        if (l < heap.length && less(heap[l], heap[top]) > 0) top = l;
        if (r < heap.length && less(heap[r], heap[top]) > 0) top = r;
        if (top === k) return;
        [heap[k], heap[top]] = [heap[top], heap[k]];
        k = top;
      }
    };
    groups.forEach((p, k) => {
      for (const m of found[k]) for (let j = 0; j < m.n; j++) {
        const x = { i: p.i, r: nth(m, j) };
        if (heap.length < want) { heap.push(x); up(heap.length - 1); } else if (less(x, heap[0]) < 0) { heap[0] = x; down(0); }
      }
    });
    picks = heap.sort(less).slice(offset);
  } else {
    let skip = offset;
    for (let k = 0; k < groups.length && picks.length < limit + 1; k++) {
      for (const m of found[k]) {
        if (skip >= m.n) { skip -= m.n; continue; }
        const take = Math.min(m.n - skip, limit + 1 - picks.length);
        for (let j = skip; j < skip + take; j++) picks.push({ i: groups[k].i, r: nth(m, j) });
        skip = 0;
        if (picks.length > limit) break;
      }
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
  const byGroup = group.map((name) => ({ name, dir: 'asc' }));
  const ordered = [...order, ...byGroup].map(sqlOrder);
  const sql = duckdbSQL(url, entry.types, specs, {
    select: [...group.map((n) => `${col(entry.types, n)} AS ${q(n)}`), ...metrics.map((x) => `${x.fn.toUpperCase()}(${x.field ? col(entry.types, x.field) : '*'}) AS ${q(x.as)}`)].join(', '),
    rest: `${group.length ? ' GROUP BY ' + group.map((n) => col(entry.types, n)).join(', ') : ''}${ordered.length ? ' ORDER BY ' + ordered.join(', ') : ''} LIMIT ${limit} OFFSET ${offset}`,
  });
  if (!entry.profiled) throw unprofiled(entry, url, sql);
  const fnames = [...new Set(specs.map((s) => s.name))];
  const mnames = metrics.filter((x) => x.field).map((x) => x.field);
  const ix = await pageIndex(env, entry, [...fnames, ...group, ...mnames]);
  const scan = new Scan(env, entry, ix, budget, refusal(entry, url, sql, budget), specs);
  const groups = prune(entry, specs, ix);
  const todo = scan.charge(needsOf(groups, [...group, ...mnames], fnames));
  const buckets = new Map();
  const fresh = () => metrics.map(() => ({ n: 0, sum: 0, c: 0, best: null }));
  let matched = 0;
  // A plain count needs no column, so a proven range is added as a number.
  const counting = !group.length && metrics.every((x) => !x.field);
  await scan.stream(groups, todo, (p) => {
    if (counting) {
      let b = buckets.get('[]');
      if (!b) buckets.set('[]', (b = { vals: [], acc: fresh() }));
      for (const m of matchesOf(scan, p, specs)) { matched += m.n; for (const a of b.acc) a.n += m.n; }
      return;
    }
    const gcols = group.map((n) => scan.col(p.i, n));
    const mcols = metrics.map((x) => (x.field ? scan.col(p.i, x.field) : null));
    eachHit(scan, p, specs, (r) => {
      matched++;
      const vals = gcols.map((c) => c[r]);
      const key = JSON.stringify(vals);
      let b = buckets.get(key);
      if (!b) {
        if (buckets.size >= budget.buckets) throw scan.refuse({ ...scan.used, buckets: buckets.size + 1 }, ['buckets']);
        buckets.set(key, (b = { vals, acc: fresh() }));
      }
      metrics.forEach((x, k) => {
        const a = b.acc[k];
        if (!x.field) { a.n++; return; }
        const v = mcols[k][r];
        if (v === null || v === undefined) return;
        a.n++;
        if (x.fn === 'sum' || x.fn === 'avg') addInto(a, Number(v));
        else if (x.fn === 'min' && (a.best === null || cmp(v, a.best) < 0)) a.best = v;
        else if (x.fn === 'max' && (a.best === null || cmp(v, a.best) > 0)) a.best = v;
      });
    });
  });
  // With no group there is one answer row, even when nothing matches, as in SQL.
  if (!group.length && !buckets.size) buckets.set('[]', { vals: [], acc: fresh() });
  let out = [...buckets.values()].map((b) => {
    const row = {};
    group.forEach((n, k) => { row[n] = b.vals[k]; });
    metrics.forEach((x, k) => {
      const a = b.acc[k];
      const total = a.sum + a.c;
      row[x.as] = x.fn === 'count' ? a.n : x.fn === 'sum' ? (a.n ? total : null) : x.fn === 'avg' ? (a.n ? total / a.n : null) : a.best;
    });
    return row;
  });
  scan.used.buckets = buckets.size;
  out.sort(sorter([...order, ...byGroup], (row, name) => row[name]));
  const more = out.length > offset + limit;
  out = out.slice(offset, offset + limit);
  return { rows: out, matched, more, cols: [...group, ...metrics.map((x) => x.as)], used: scan.used, sql };
}

