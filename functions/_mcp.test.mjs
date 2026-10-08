import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { test } from 'node:test';
import vm from 'node:vm';
import { answer } from './_api.js';
import { aggregateQuery, rowsQuery } from './_query.js';
import { onRequestGet, onRequestPost } from './mcp.js';
import { onRequestGet as voteCounts } from './api/v1/votes.js';
import SPEC from './_tools.json' with { type: 'json' };

// Runs the MCP endpoint against a D1 stand-in over node:sqlite, through the same answer() the
// public API uses, and runs site.js against the same data to hold the two surfaces to each other.
const fields = [
  { name: 'lga', type: 'string' },
  { name: 'year', type: 'integer' },
  { name: 'fatal', type: 'boolean' },
];
const sql = new DatabaseSync(':memory:');
sql.exec(
  'CREATE TABLE _versions (slug TEXT, version TEXT, tbl TEXT, fields TEXT, rows INTEGER, attribution TEXT, header TEXT)',
);
sql.exec('CREATE TABLE t (lga TEXT, year INTEGER, fatal INTEGER)');
sql
  .prepare('INSERT INTO _versions VALUES (?, ?, ?, ?, ?, ?, ?)')
  .run(
    'crashes',
    '2026-04-24',
    't',
    JSON.stringify(fields),
    4,
    'A',
    JSON.stringify({ attribution: 'A' }),
  );
const ins = sql.prepare('INSERT INTO t VALUES (?, ?, ?)');
for (const r of [
  ['Gold Coast', 2020, 1],
  ['Gold Coast', 2021, 0],
  ['Brisbane', 2020, 0],
  ['Logan', null, 1],
]) {
  ins.run(...r);
}

const DB = {
  prepare(q) {
    let binds = [];
    const st = {
      bind(...b) {
        binds = b;
        return st;
      },
      async first() {
        const r = sql.prepare(q).get(...binds);
        return r ? { ...r } : null;
      },
      async all() {
        return {
          results: sql
            .prepare(q)
            .all(...binds)
            .map((r) => ({ ...r })),
        };
      },
    };
    return st;
  },
};
const assets = {
  '/catalog.json': {
    dataset: [
      {
        identifier: 'crashes',
        title: 'Crashes',
        publisher: { name: 'TMR' },
        license: 'CC-BY-4.0',
        landingPage: 'https://publicdata.au/d/crashes/',
      },
    ],
  },
  '/latest.json': { crashes: '2026-04-24' },
  '/d/crashes/datapackage.json': { name: 'x' },
  '/d/crashes/versions.json': [{ version: '2026-04-24' }],
  '/d/crashes/v/2026-04-24/by/lga/index.json': {
    field: 'lga',
    partitions: [{ value: 'Logan', rows: 1, json: 'by/lga/Logan.json' }],
  },
  '/d/crashes/diff/2026-01-01..2026-04-24.json': {
    dataset: 'crashes',
    from: '2026-01-01',
    to: '2026-04-24',
    rows_from: 3,
    rows_to: 4,
    key: ['id'],
    schema: {},
    added: 1,
    removed: 0,
    changed: 2,
  },
  '/mcp/resources.json': {
    resources: [
      {
        uri: 'https://publicdata.au/d/crashes/fields.json',
        name: 'crashes',
        title: 'Crashes',
        description: 'd',
        mimeType: 'application/json',
      },
    ],
  },
  '/d/crashes/fields.json': {
    slug: 'crashes',
    version: '2026-04-24',
    key: ['id'],
    partition_by: ['lga'],
    fields,
  },
  '/backlog.json': {
    entries: [
      { slug: 'wanted', title: 'Wanted', status: 'backlog', publisher: { name: 'P' } },
      { slug: 'soon', title: 'Soon', status: 'building' },
      { slug: 'crashes', title: 'Crashes', status: 'live' },
    ],
  },
};
const votes = new Map();
const env = {
  DB,
  ASSETS: {
    fetch: async (r) => {
      const p = new URL(r.url || r).pathname;
      return p in assets ? Response.json(assets[p]) : new Response('', { status: 404 });
    },
  },
  DIST: { head: async () => null, get: async () => null },
  VOTES: {
    get: async (k) => (votes.has(k) ? { text: async () => votes.get(k) } : null),
    put: async (k, v) => {
      votes.set(k, v);
    },
    head: async (k) => (votes.has(k) ? {} : null),
    list: async ({ prefix }) => ({
      objects: [...votes.keys()].filter((k) => k.startsWith(prefix)).map((key) => ({ key })),
      truncated: false,
    }),
  },
};
// Only the rate counter is kept, so every query runs rather than coming from the cache.
const kept = new Map();
globalThis.caches = {
  default: {
    match: async (r) => (kept.has(r.url) ? new Response(kept.get(r.url)) : undefined),
    put: async (r, res) => {
      if (new URL(r.url).pathname.startsWith('/_limit/')) {
        kept.set(r.url, await res.text());
      }
    },
  },
};

let id = 0;
async function rpc(method, params, headers = {}) {
  const request = new Request('https://publicdata.au/mcp', {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...headers },
    body: JSON.stringify({ jsonrpc: '2.0', id: ++id, method, params }),
  });
  const r = await onRequestPost({ request, env, waitUntil() {} });
  return { status: r.status, body: r.status === 202 ? null : await r.json() };
}
const tool = async (name, args) => (await rpc('tools/call', { name, arguments: args })).body.result;
const out = async (name, args) => JSON.parse((await tool(name, args)).content[0].text);

// site.js with its fetch sent to the same handlers, as the browser's would be.
const raw = JSON.parse(
  readFileSync(new URL('../pipeline/publicdata/api.json', import.meta.url), 'utf8'),
);
const fill = (v) =>
  typeof v === 'string'
    ? v
        .replace(/\{([a-z_]+)\}/g, (m, k) => (k in raw.limits ? String(raw.limits[k]) : m))
        .replace(/`([^`]+)`/g, '$1')
    : Array.isArray(v)
      ? v.map(fill)
      : v && typeof v === 'object'
        ? Object.fromEntries(Object.entries(v).map(([k, x]) => [k, fill(x)]))
        : v;
const pageFetch = async (u) => {
  const url = new URL(u, 'https://publicdata.au');
  const m = url.pathname.match(
    /^\/api\/v1\/datasets\/([^/]+)\/(?:versions\/([^/]+)\/)?(rows|aggregate)$/,
  );
  if (url.pathname === '/api/v1/votes') {
    return voteCounts({ env, waitUntil() {} });
  }
  if (!m) {
    return env.ASSETS.fetch(new Request(url));
  }
  return answer(
    { request: new Request(url), env, params: { slug: m[1], version: m[2] }, waitUntil() {} },
    m[3] === 'rows' ? rowsQuery : aggregateQuery,
    m[3],
  );
};
const browserSpec = {
  operators: {},
  parameters: Object.fromEntries(
    Object.entries(raw.parameters).map(([k, p]) => [
      k,
      {
        description: fill(
          p.on
            ? `On \`${p.on}\`, ${p.description[0].toLowerCase()}${p.description.slice(1)}`
            : p.description,
        ),
      },
    ]),
  ),
  webmcp: fill(raw.webmcp),
};
browserSpec.operators = fill(raw.operators);
const src = readFileSync(
  new URL('../pipeline/publicdata/static/site.js', import.meta.url),
  'utf8',
).replace('/*API_SPEC*/null', JSON.stringify(browserSpec));
const page = {};
vm.runInNewContext(src, {
  document: { getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] },
  location: { origin: 'https://publicdata.au', href: '' },
  localStorage: { getItem: () => null, setItem() {} },
  navigator: {
    modelContext: {
      registerTool: (t) => {
        page[t.name] = t;
      },
    },
  },
  fetch: pageFetch,
  Promise,
  JSON,
  Error,
  String,
  Object,
  Array,
  encodeURIComponent,
});
const plain = (v) => JSON.parse(JSON.stringify(v));

// The part of JSON Schema the output schemas use: type, properties, required and items.
function conforms(schema, v, at = '$') {
  const types = [].concat(schema.type || []);
  const kind =
    v === null ? 'null' : Array.isArray(v) ? 'array' : Number.isInteger(v) ? 'integer' : typeof v;
  if (types.length && !types.includes(kind) && !(kind === 'integer' && types.includes('number'))) {
    return `${at} is ${kind}, not ${types.join(' or ')}`;
  }
  for (const k of schema.required || []) {
    if (!(k in v)) {
      return `${at}.${k} is missing`;
    }
  }
  for (const [k, s] of Object.entries(schema.properties || {})) {
    if (v && k in v) {
      const e = conforms(s, v[k], `${at}.${k}`);
      if (e) {
        return e;
      }
    }
  }
  if (schema.items && Array.isArray(v)) {
    for (const [i, x] of v.entries()) {
      const e = conforms(schema.items, x, `${at}[${i}]`);
      if (e) {
        return e;
      }
    }
  }
  return null;
}

test('initialize names the server and settles the protocol version', async () => {
  const r = (
    await rpc('initialize', {
      protocolVersion: '2025-06-18',
      capabilities: {},
      clientInfo: { name: 't', version: '1' },
    })
  ).body.result;
  assert.equal(r.protocolVersion, '2025-06-18');
  assert.equal(r.serverInfo.name, 'publicdata-au');
  assert.deepEqual(r.capabilities.tools, { listChanged: false });
  assert.equal(r.serverInfo.version, '0.0.0-dev');
  assert.equal(r.instructions, fill(raw.mcp.instructions));
  assert.equal(
    (await rpc('initialize', { protocolVersion: '1999-01-01' })).body.result.protocolVersion,
    '2025-11-25',
  );
  assert.equal((await rpc('ping', {}, { 'mcp-protocol-version': '2025-06-18' })).status, 200);
  assert.equal((await rpc('ping', {}, { 'mcp-protocol-version': '1999-01-01' })).status, 400);
});

test('tools/list is every tool in api.json with the schema the pages register', async () => {
  const { tools } = (await rpc('tools/list')).body.result;
  assert.deepEqual(
    tools.map((t) => t.name),
    Object.keys(raw.webmcp.tools),
  );
  for (const t of tools) {
    assert.equal(t.description, page[t.name].description, t.name);
    assert.equal(t.title, page[t.name].title, t.name);
    assert.deepEqual(t.inputSchema, plain(page[t.name].inputSchema), t.name);
    assert.deepEqual(t.annotations ?? null, plain(page[t.name].annotations ?? null), t.name);
  }
});

test('every tool answers exactly as its twin in the page does', async () => {
  const calls = [
    ['search_datasets', { query: 'crash' }],
    ['get_dataset', { slug: 'crashes' }],
    ['list_fields', { slug: 'crashes' }],
    ['list_partitions', { slug: 'crashes', field: 'lga' }],
    ['query_rows', { slug: 'crashes', where: { lga: { like: '*gold*' }, year: { min: 2021 } } }],
    [
      'query_rows',
      {
        slug: 'crashes',
        where: { lga: ['Brisbane', 'Logan'] },
        select: ['lga'],
        order: 'lga.desc',
      },
    ],
    ['query_rows', { slug: 'crashes', where: { year: null, fatal: true } }],
    ['query_rows', { slug: 'crashes', limit: 3, offset: 0 }],
    [
      'count_rows',
      { slug: 'crashes', group_by: ['lga'], where: { year: { min: 2020, max: 2021 } } },
    ],
    [
      'count_rows',
      { slug: 'crashes', metric: 'sum.fatal', group_by: ['lga'], version: '2026-04-24' },
    ],
    ['diff_versions', { slug: 'crashes', from: '2026-01-01', to: '2026-04-24' }],
    ['list_backlog', {}],
  ];
  for (const [name, args] of calls) {
    const r = await tool(name, args);
    assert.ok(!r.isError, `${name} ${r.content[0].text}`);
    assert.equal(r.content[0].text, await page[name].execute(args), name);
    assert.deepEqual(r.structuredContent, JSON.parse(r.content[0].text));
    const schema = SPEC.tools.find((x) => x.name === name).outputSchema;
    assert.equal(conforms(schema, r.structuredContent), null, name);
  }
  const s = await out('count_rows', {
    slug: 'crashes',
    group_by: ['lga'],
    where: { year: { min: 2020, max: 2021 } },
  });
  assert.deepEqual(s.groups, [
    { lga: 'Gold Coast', count: 2 },
    { lga: 'Brisbane', count: 1 },
  ]);
  assert.equal(s.matched, 3);
  assert.equal((await out('query_rows', { slug: 'crashes', limit: 3 })).next_offset, 3);
});

test('a bad call comes back as a tool error the model can read', async () => {
  const err = async (name, args) => {
    const r = await tool(name, args);
    assert.equal(r.isError, true, name);
    return r.content[0].text;
  };
  assert.match(await err('query_rows', {}), /slug is required/);
  assert.match(
    await err('query_rows', { slug: 'crashes', where: { lga: ['a,b'] } }),
    /cannot contain a comma/,
  );
  assert.match(await err('query_rows', { slug: 'crashes', where: { nope: 1 } }), /no field nope/);
  assert.match(await err('count_rows', { slug: 'crashes', colour: 'red' }), /not colour/);
  assert.match(await err('get_dataset', { slug: 'missing' }), /404/);
  assert.match(
    await err('list_fields', { slug: 'missing' }),
    /no queryable dataset with slug missing/,
  );
  assert.match(
    (await page.list_fields.execute({ slug: 'missing' }).catch((e) => e)).message,
    /no queryable dataset with slug missing/,
  );
  assert.match(await err('query_rows', { slug: 'crashes', version: '2020-01-01' }), /not loaded/);
  const r = await rpc('tools/call', { name: 'drop_tables', arguments: {} });
  assert.equal(r.body.error.code, -32602);
});

test('upvote_dataset counts once a day for the caller', async () => {
  assert.equal((await out('upvote_dataset', { slug: 'wanted' })).votes, 1);
  votes.delete('_counts.json'); // the rollup from an earlier test is under a minute old
  const b = await out('list_backlog', {});
  assert.deepEqual(
    b.entries.map((e) => [e.slug, e.votes]),
    [
      ['wanted', 1],
      ['soon', 0],
    ],
  );
  assert.deepEqual(b.catalogue_votes, []);
  assert.equal((await out('upvote_dataset', { slug: 'wanted' })).votes, 1);
  assert.match(
    (await tool('upvote_dataset', { slug: 'crashes' })).content[0].text,
    /not open for votes/,
  );
});

test('the row tools stop at the query API limit for one address and say how long to wait', async (t) => {
  t.mock.method(Date, 'now', () => 1_700_000_003_000);
  const at = { 'cf-connecting-ip': '198.51.100.7' };
  const count = async (headers) =>
    (await rpc('tools/call', { name: 'count_rows', arguments: { slug: 'crashes' } }, headers)).body
      .result;
  const per = raw.limits.requests / 2;
  for (let i = 0; i < per; i++) {
    assert.ok(!(await count(at)).isError, String(i));
  }
  const r = await count(at);
  assert.equal(r.isError, true);
  assert.equal(r.content[0].text, 'rate limited; wait 7 seconds and call again');
  // search_datasets reads D1 too, so it shares the limit.
  assert.ok(
    (await rpc('tools/call', { name: 'search_datasets', arguments: { query: 'x' } }, at)).body
      .result.isError,
  );
  assert.ok(!(await count({ 'cf-connecting-ip': '198.51.100.8' })).isError);
});

test('each dataset is a resource the agent can read without a tool call', async () => {
  const init = (await rpc('initialize', { protocolVersion: '2025-06-18' })).body.result;
  assert.deepEqual(init.capabilities.resources, { listChanged: false, subscribe: false });
  const { resources } = (await rpc('resources/list')).body.result;
  assert.deepEqual(
    resources.map((r) => r.uri),
    ['https://publicdata.au/d/crashes/fields.json'],
  );
  const { contents } = (await rpc('resources/read', { uri: resources[0].uri })).body.result;
  assert.equal(contents[0].mimeType, 'application/json');
  assert.deepEqual(JSON.parse(contents[0].text).fields, fields);
  const { resourceTemplates } = (await rpc('resources/templates/list')).body.result;
  assert.equal(resourceTemplates[0].uriTemplate, 'https://publicdata.au/d/{slug}/fields.json');
  assert.equal(
    (await rpc('resources/read', { uri: 'https://publicdata.au/d/missing/fields.json' })).body.error
      .code,
    -32002,
  );
  assert.equal(
    (await rpc('resources/read', { uri: 'file:///etc/passwd' })).body.error.code,
    -32602,
  );
});

test('prompts attach a dataset and completions suggest slugs', async () => {
  const { prompts } = (await rpc('prompts/list')).body.result;
  assert.deepEqual(
    prompts.map((p) => p.name),
    Object.keys(raw.mcp.prompts),
  );
  const g = (
    await rpc('prompts/get', {
      name: 'ask_dataset',
      arguments: { slug: 'crashes', question: 'How many in 2020?' },
    })
  ).body.result;
  assert.match(g.messages[0].content.text, /dataset crashes\..*Question: How many in 2020\?$/s);
  assert.equal(g.messages[1].content.type, 'resource');
  assert.deepEqual(JSON.parse(g.messages[1].content.resource.text).fields, fields);
  const bare = (await rpc('prompts/get', { name: 'ask_dataset', arguments: { slug: 'crashes' } }))
    .body.result;
  assert.doesNotMatch(bare.messages[0].content.text, /Question/);
  assert.equal(
    (await rpc('prompts/get', { name: 'ask_dataset', arguments: {} })).body.error.code,
    -32602,
  );
  assert.equal(
    (await rpc('prompts/get', { name: 'ask_dataset', arguments: { slug: 'missing' } })).body.error
      .code,
    -32602,
  );
  const c = (
    await rpc('completion/complete', {
      ref: { type: 'ref/resource', uri: 'https://publicdata.au/d/{slug}/fields.json' },
      argument: { name: 'slug', value: 'cra' },
    })
  ).body.result.completion;
  assert.deepEqual(c.values, ['crashes']);
  const p = (
    await rpc('completion/complete', {
      ref: { type: 'ref/prompt', name: 'ask_dataset' },
      argument: { name: 'slug', value: 'zzz' },
    })
  ).body.result.completion;
  assert.deepEqual(p.values, []);
  const t = (
    await rpc('completion/complete', {
      ref: { type: 'ref/prompt', name: 'find_data' },
      argument: { name: 'slug', value: 'cra' },
    })
  ).body.result.completion;
  assert.deepEqual(t.values, []);
});

test('JSON-RPC edges: notifications, unknown methods, bad bodies and GET', async () => {
  const note = await onRequestPost({
    request: new Request('https://publicdata.au/mcp', {
      method: 'POST',
      body: JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }),
    }),
    env,
    waitUntil() {},
  });
  assert.equal(note.status, 202);
  assert.equal((await rpc('logging/setLevel', { level: 'info' })).body.error.code, -32601);
  const bad = await onRequestPost({
    request: new Request('https://publicdata.au/mcp', { method: 'POST', body: '{' }),
    env,
    waitUntil() {},
  });
  assert.equal(bad.status, 400);
  assert.equal(bad.headers.get('link'), '<https://publicdata.au/terms/>; rel="terms-of-service"');
  assert.equal((await bad.json()).error.code, -32700);
  assert.equal(onRequestGet().status, 405);
});
