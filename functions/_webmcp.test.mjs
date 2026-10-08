import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { test } from 'node:test';
import vm from 'node:vm';
import { aggregateQuery, rowsQuery } from './_query.js';

// Runs the page script with its tools' fetch routed through the real query builders, so the
// where-to-operator mapping is tested against SQL rather than against URL strings.
const fields = [
  { name: 'lga', type: 'string' },
  { name: 'year', type: 'integer' },
  { name: 'fatal', type: 'boolean' },
];
const db = new DatabaseSync(':memory:');
db.exec('CREATE TABLE t (lga TEXT, year INTEGER, fatal INTEGER)');
const ins = db.prepare('INSERT INTO t VALUES (?, ?, ?)');
for (const r of [['Gold Coast', 2020, 1], ['Gold Coast', 2021, 0], ['Brisbane', 2020, 0], ['Logan', null, 1]]) ins.run(...r);

const seen = [];
async function fakeFetch(u) {
  const url = new URL(u, 'https://publicdata.au');
  seen.push(url.pathname + url.search);
  const op = url.pathname.split('/').pop();
  const plan = (op === 'rows' ? rowsQuery : aggregateQuery)('t', fields, url.searchParams);
  let rows = db.prepare(plan.sql).all(...plan.binds).map((r) => ({ ...r }));
  const more = rows.length > plan.limit;
  if (more) rows = rows.slice(0, plan.limit);
  const body = { publicdata: { attribution: 'A' }, rows, next: more ? 'n' : null };
  return new Response(JSON.stringify(body), { headers: { 'x-publicdata-version': '2026-04-24' } });
}

// The build fills the spec slot from api.json; this does the same without Python.
const raw = JSON.parse(readFileSync(new URL('../pipeline/publicdata/api.json', import.meta.url), 'utf8'));
const fill = (v) => typeof v === 'string' ? v.replace(/\{([a-z_]+)\}/g, (m, k) => (k in raw.limits ? String(raw.limits[k]) : m)).replace(/`([^`]+)`/g, '$1')
  : Array.isArray(v) ? v.map(fill) : v && typeof v === 'object' ? Object.fromEntries(Object.entries(v).map(([k, x]) => [k, fill(x)])) : v;
const spec = fill({ operators: raw.operators, parameters: raw.parameters, webmcp: raw.webmcp });
const src = readFileSync(new URL('../pipeline/publicdata/static/site.js', import.meta.url), 'utf8').replace('/*API_SPEC*/null', JSON.stringify(spec));

function load(dsData, fetcher = fakeFetch) {
  const tools = {};
  const byId = { 'ds-data': dsData ? { textContent: JSON.stringify(dsData) } : null };
  vm.runInNewContext(src, {
    document: { getElementById: (id) => byId[id] || null, querySelector: () => null, querySelectorAll: () => [] },
    location: { origin: 'https://publicdata.au', href: '' },
    localStorage: { getItem: () => null, setItem() {} },
    navigator: { modelContext: { registerTool: (t) => { tools[t.name] = t; } } },
    fetch: fetcher,
    Promise, JSON, Error, String, Object, Array, encodeURIComponent,
  });
  return tools;
}
const tools = load(null);
const call = async (name, input) => JSON.parse(await tools[name].execute(input));

test('query_rows maps every where shape onto the API and counts the whole match', async () => {
  const r = await call('query_rows', { slug: 'x', where: { lga: { like: '*gold*' }, year: { min: 2021 } } });
  assert.deepEqual(r.rows, [{ lga: 'Gold Coast', year: 2021, fatal: 0 }]);
  assert.equal(r.matched, 1);
  assert.equal(r.version, '2026-04-24');
  assert.equal((await call('query_rows', { slug: 'x', where: { lga: ['Brisbane', 'Logan'] } })).matched, 2);
  assert.equal((await call('query_rows', { slug: 'x', where: { year: null } })).rows[0].lga, 'Logan');
  assert.equal((await call('query_rows', { slug: 'x', where: { fatal: true } })).matched, 2);
});

test('query_rows pages with next_offset', async () => {
  const a = await call('query_rows', { slug: 'x', limit: 3 });
  assert.equal(a.next_offset, 3);
  const b = await call('query_rows', { slug: 'x', limit: 3, offset: 3 });
  assert.equal(b.next_offset, null);
  assert.equal(b.rows.length, 1);
});

test('count_rows groups, sorts by the metric and totals the filtered rows', async () => {
  const r = await call('count_rows', { slug: 'x', group_by: ['lga'], where: { year: { min: 2020, max: 2021 } } });
  assert.deepEqual(r.groups, [{ lga: 'Gold Coast', count: 2 }, { lga: 'Brisbane', count: 1 }]);
  assert.equal(r.matched, 3);
  // Equal totals are ordered by group, so every engine and the cited query give the same top groups.
  assert.match(r.query, /order=count\.desc,lga\.asc&/);
  const s = await call('count_rows', { slug: 'x', metric: 'sum.fatal', group_by: ['lga'], version: '2026-04-24' });
  assert.equal(s.groups[0].sum_fatal, 1);
  assert.ok(seen.some((p) => p.startsWith('/api/v1/datasets/x/versions/2026-04-24/aggregate')));
});

test('a list value with a comma rejects instead of throwing', async () => {
  for (const name of ['query_rows', 'count_rows']) {
    const p = tools[name].execute({ slug: 'x', where: { lga: ['Gold Coast, City'] } });
    assert.ok(p instanceof Promise);
    await assert.rejects(p, /cannot contain a comma/);
  }
});

test('every tool in api.json registers with its text and an executor', () => {
  assert.deepEqual(Object.keys(tools), Object.keys(raw.webmcp.tools));
  for (const [name, t] of Object.entries(tools)) {
    assert.equal(typeof t.execute, 'function', name);
    assert.ok(t.description.startsWith(fill(raw.webmcp.tools[name].description)), name);
    assert.deepEqual(Object.keys(t.inputSchema.properties), Object.keys(raw.webmcp.tools[name].input), name);
  }
  assert.equal(tools.query_rows.inputSchema.properties.order.description, fill(raw.parameters.order.description));
});

test('on a dataset page the row tools default the slug and describe each field', async () => {
  const page = load({ slug: 'x', title: 'Crashes', console: { fields: [
    { name: 'lga', type: 'string', description: 'Council area.', values: ['Brisbane', 'Gold Coast', 'Logan'] },
    { name: 'year', type: 'integer', min: 2020, max: 2021 },
  ] } });
  const s = page.query_rows.inputSchema;
  assert.ok(!s.required.includes('slug'));
  assert.equal(s.properties.slug.default, 'x');
  assert.match(page.query_rows.description, /defaults to x \(Crashes\)/);
  assert.deepEqual(JSON.parse(JSON.stringify(s.properties.where.properties.lga.anyOf[0])), { enum: ['Brisbane', 'Gold Coast', 'Logan'] });
  assert.match(s.properties.where.properties.lga.description, /^Council area\. Type string\.$/);
  assert.match(s.properties.where.properties.year.description, /From 2020 to 2021/);
  assert.ok(page.get_dataset.inputSchema.required.includes('slug'));
  const r = JSON.parse(await page.count_rows.execute({ group_by: ['lga'], where: { lga: 'Logan' } }));
  assert.deepEqual(r.groups, [{ lga: 'Logan', count: 1 }]);
});

test('a 429 tells the agent how long to wait, and any other failure carries the API error', async () => {
  const fail = (status, body, headers = {}) => load(null, async () => new Response(JSON.stringify(body), { status, headers }));
  await assert.rejects(fail(429, { error: 'x' }, { 'retry-after': '7' }).query_rows.execute({ slug: 'x' }), /wait 7 seconds/);
  await assert.rejects(fail(400, { error: 'no field nope' }).count_rows.execute({ slug: 'x' }), /no field nope/);
});

// Just enough DOM for the query builder to build its controls and write its URL.
class FakeEl {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.attrs = {}; this.dataset = {}; this.on = {}; this.hidden = false; this.checked = false; this.selected = false; this.textContent = ''; this._value = ''; }
  get value() {
    if (this.tagName !== 'SELECT' || this._set) return this._value;
    const o = this.children.find((c) => c.selected) || this.children[0];
    return o ? o.value : '';
  }
  set value(v) { this._value = String(v); this._set = this.tagName === 'SELECT'; }
  set innerHTML(_) { this.children = []; this._set = false; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k.startsWith('data-')) this.dataset[k.slice(5)] = String(v); if (k === 'value') this.value = v; }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  appendChild(c) { this.children.push(c); return c; }
  addEventListener(type, f) { (this.on[type] ||= []).push(f); }
  dispatchEvent(e) { (this.on[e.type] || []).forEach((f) => f(e)); }
  querySelectorAll(sel) {
    const [tag, checked] = sel.split(':');
    const out = [];
    const walk = (n) => n.children.forEach((c) => { if (c.tagName === tag.toUpperCase() && (!checked || c.checked)) out.push(c); walk(c); });
    walk(this);
    return out;
  }
  focus() {}
  remove() {}
}

test('the query builder keeps literal commas and colons in its URLs beside the WebMCP tools', () => {
  const made = [];
  const ds = { slug: 'x', title: 'Crashes', console: {
    fields: [{ name: 'lga', type: 'string' }, { name: 'time', type: 'string' }],
    example: { filters: [{ field: 'lga', op: 'in', value: 'Gold Coast,Logan' }, { field: 'time', op: 'eq', value: '12:30' }], group: [], metric: 'count' },
    versions: ['2026-04-24'], api: '/api/v1/datasets/x/', site: 'https://publicdata.au',
  } };
  const byId = { console: new FakeEl('div'), 'ds-data': { textContent: JSON.stringify(ds) } };
  vm.runInNewContext(src, {
    document: { getElementById: (id) => byId[id] || null, createElement: (t) => { const n = new FakeEl(t); made.push(n); return n; }, querySelector: () => null, querySelectorAll: () => [] },
    location: { origin: 'https://publicdata.au', href: '', search: '' },
    localStorage: { getItem: () => null, setItem() {} },
    navigator: { modelContext: { registerTool() {} } },
    Event: class { constructor(type) { this.type = type; } },
    fetch: fakeFetch,
  });
  const url = made.find((n) => n.attrs.id === 'q-url');
  const format = made.find((n) => n.attrs['aria-label'] === 'Format');
  format.value = 'csv';
  format.dispatchEvent({ type: 'change' });
  assert.match(url.textContent, /format=csv/);
  assert.match(url.textContent, /lga=in\.\(Gold%20Coast,Logan\)&time=eq\.12:30&/);
});
