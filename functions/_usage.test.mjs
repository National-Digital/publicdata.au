import assert from 'node:assert/strict';
import { test } from 'node:test';
import { onRequestGet, onRequestHead, onRequestPost } from './mcp.js';
import { line } from './_usage.js';

// The usage line for each MCP request, read from console.log as the Functions log would read it.
// Only calls that answer before any data is read are made, so no D1 or asset stand-in is needed.
const AGENT = 'claude-code/2.1.0 (external, cli)';
const post = (body, headers = {}, env = {}) =>
  onRequestPost({
    request: new Request('https://publicdata.au/mcp', {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'user-agent': AGENT, ...headers },
      body: typeof body === 'string' ? body : JSON.stringify(body),
    }),
    env,
    waitUntil() {},
  });
const rpc = (id, method, params) => ({ jsonrpc: '2.0', id, method, params });
const init = (clientInfo) => rpc(1, 'initialize', { protocolVersion: '2025-06-18', clientInfo });

async function logged(t, run) {
  const seen = [];
  t.mock.method(console, 'log', (s) => seen.push(s));
  const res = await run();
  t.mock.restoreAll();
  return { res, lines: seen.map((s) => JSON.parse(s)), raw: seen.join('\n') };
}
const one = async (t, ...a) => (await logged(t, () => post(...a))).lines;
const req = (ua) =>
  new Request('https://publicdata.au/mcp', {
    method: 'POST',
    headers: ua === undefined ? {} : { 'user-agent': ua },
  });
const of = (msg, ua = 'node') => line(req(ua), [[msg, null]], 200);

test('initialize records a known client, its version and the agent', async (t) => {
  const lines = await one(t, init({ name: 'claude-code', version: '2.1.296' }));
  assert.deepEqual(lines, [
    {
      event: 'mcp',
      http: 'POST',
      status: 200,
      ua: 'claude-code',
      protocol: null,
      session: false,
      batch: false,
      calls: 1,
      methods: { initialize: 1 },
      client: 'claude-code',
      client_version: '2.1.296',
      class: 'client',
    },
  ]);
  const later = await logged(t, () =>
    post(rpc(2, 'tools/list'), {
      'mcp-protocol-version': '2025-06-18',
      'mcp-session-id': 'abc123',
    }),
  );
  assert.equal(later.lines[0].protocol, '2025-06-18');
  assert.equal(later.lines[0].session, true);
  assert.ok(!later.raw.includes('abc123'), 'the session id is never written');
});

test('no free text a caller sends reaches the line', () => {
  const secrets = [
    'Jane Smith',
    '+61 412 345 678',
    '192.168.0.4',
    // Token shapes are built here so no commit holds a string a secret scanner takes for one.
    ['ghp', 'A1b2'.repeat(9)].join('_'),
    'jane @example.com',
    'jane.smith@exam',
    ['sk', 'ant', 'api03', 'AbCdEf'].join('-'),
  ];
  for (const s of secrets) {
    const r = JSON.stringify([
      of(rpc(1, 'initialize', { clientInfo: { name: s, version: s } }), `${s}/1.0`),
      of(rpc(1, 'initialize', { clientInfo: { name: 'claude-code', version: `1.2.3 ${s}` } })),
      of(rpc(1, s)),
      of(rpc(1, 'tools/call', { name: s, arguments: { [s]: s } })),
      of(rpc(1, 'tools/call', { name: 'query_rows', arguments: { slug: s, [s]: s } })),
      of(rpc(1, 'prompts/get', { name: 'find_data', arguments: { topic: s } })),
    ]);
    for (const part of s.split(/[\s@.+-]+/).filter((p) => p.length > 2)) {
      assert.ok(!r.includes(part), `${part} of ${s} is not written`);
    }
  }
  const c = of(rpc(1, 'initialize', { clientInfo: { name: 'Jane Smith', version: '1.0.0' } }));
  assert.equal(c.client, 'other');
  assert.equal(c.client_version, undefined, 'an unknown client has no version');
  assert.equal(of(rpc(1, 'x'), `${['sk', 'ant', 'api03', 'AbCdEf'].join('-')}/1.0`).ua, 'other');
});

test('a client name, version and agent are kept only from the lists', () => {
  const c = (name, version) => of(rpc(1, 'initialize', { clientInfo: { name, version } }));
  assert.deepEqual(
    [c('mcp-inspector', '0.17.2').client, c('mcp-inspector', '0.17.2').client_version],
    ['mcp-inspector', '0.17.2'],
  );
  assert.equal(c('claude-code', 'v2.1.0-beta.3').client_version, '2.1.0');
  assert.equal(c('claude-code', '2.1').client_version, undefined);
  const via = c('claude-code (via mcp-remote 0.1.29)', '2.1.0');
  assert.deepEqual([via.client, via.proxy], ['claude-code', 'mcp-remote']);
  assert.equal(c('Jane (via mcp-remote 0.1.29)', '1.0.0').client, 'other');
  assert.equal(of(rpc(1, 'x'), 'python-httpx/0.28.1').ua, 'python-httpx');
  assert.equal(
    of(
      rpc(1, 'x'),
      'Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0; +https://openai.com/bot',
    ).ua,
    'ChatGPT-User',
  );
  assert.equal(of(rpc(1, 'x'), 'Mozilla/5.0 (Windows NT 10.0) Firefox/131.0').ua, 'Mozilla');
  assert.equal(of(rpc(1, 'x'), '').ua, null);
});

test('a tool call records the tool and its argument names', async (t) => {
  const { res, lines, raw } = await logged(t, () =>
    post(
      rpc(3, 'tools/call', {
        name: 'search_datasets',
        arguments: { query: 'crashes near 12 Smith St', bogus: 1 },
      }),
      { 'cf-connecting-ip': '203.0.113.9', authorization: 'Bearer s3cret' },
    ),
  );
  assert.equal((await res.json()).result.isError, true);
  assert.deepEqual(lines[0].tools, { search_datasets: 1 });
  assert.deepEqual(lines[0].args, { search_datasets: ['other', 'query'] });
  assert.deepEqual(lines[0].errors, { tool: 1 });
  for (const s of ['Smith', 'bogus', '203.0.113.9', 's3cret']) {
    assert.ok(!raw.includes(s), `${s} is not written`);
  }
});

test('a batch writes one line however many calls it holds', async (t) => {
  const many = [...Array(500).keys()].map((i) =>
    rpc(i, 'tools/call', { name: 'search_datasets', arguments: { bogus: i } }),
  );
  const { res, lines, raw } = await logged(t, () =>
    post([
      rpc(1, 'ping'),
      { jsonrpc: '2.0', method: 'notifications/initialized' },
      rpc(2, 'prompts/get', { name: 'find_data', arguments: { topic: 'secret', extra: 1 } }),
      ...many,
      'junk',
    ]),
  );
  assert.equal(res.status, 200);
  assert.equal(lines.length, 1);
  const [l] = lines;
  assert.equal(l.batch, true);
  assert.equal(l.calls, 504);
  assert.deepEqual(l.methods, {
    ping: 1,
    'notifications/initialized': 1,
    'prompts/get': 1,
    'tools/call': 500,
  });
  assert.deepEqual(l.tools, { search_datasets: 500 });
  assert.deepEqual(l.prompts, { find_data: 1 });
  assert.deepEqual(l.args, { find_data: ['other', 'topic'], search_datasets: ['other'] });
  assert.deepEqual(l.errors, { invalid: 1, tool: 500 });
  assert.equal(l.class, 'client');
  assert.ok(raw.length < 1000, 'the line stays short');
});

test('probes are told apart from clients on evidence only', async (t) => {
  const cls = async (body, headers) => (await one(t, body, headers))[0].class;
  assert.equal(await cls(init({ name: 'cursor-vscode', version: '1.0' })), 'client');
  assert.equal(await cls(init({ name: 'x', version: '1' }), { 'user-agent': '' }), 'client');
  assert.equal(
    await cls(init({ name: 'x', version: '1' }), {
      'user-agent':
        'Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0; +https://openai.com/bot',
    }),
    'client',
  );
  assert.equal(await cls(rpc(1, 'logging/setLevel', { level: 'info' })), 'client');
  const old = await one(t, rpc(1, 'ping'), { 'mcp-protocol-version': '1999-01-01' });
  assert.deepEqual(
    [old[0].status, old[0].class, old[0].errors, old[0].protocol],
    [400, 'client', { protocol: 1 }, 'other'],
  );
  assert.equal(
    await cls(init({ name: 'x', version: '1' }), {
      'user-agent': 'Mozilla/5.0 (compatible; CensysInspect/1.1)',
    }),
    'probe',
  );
  assert.equal(await cls(rpc(1, 'tools/list'), { 'user-agent': 'curl/8.5.0' }), 'probe');
  assert.equal(await cls(init(undefined)), 'probe');
  assert.equal(await cls(init({ name: 'mcp-scanner', version: '1' })), 'probe');
  assert.equal(await cls('{'), 'probe');
  assert.equal(await cls({ id: 1, method: 'tools/list' }), 'probe');
  assert.equal(await cls([]), 'probe');
  for (const [handler, method] of [
    [onRequestGet, 'GET'],
    [onRequestHead, 'HEAD'],
  ]) {
    const { res, lines } = await logged(t, () =>
      handler({
        request: new Request('https://publicdata.au/mcp', {
          method,
          headers: { 'user-agent': AGENT },
        }),
      }),
    );
    assert.equal(res.status, 405);
    assert.deepEqual(
      [lines[0].http, lines[0].calls, lines[0].status, lines[0].class],
      [method, 0, 405, 'probe'],
    );
  }
});

test('a call that throws is written as internal and still throws', async (t) => {
  // No JSON body makes the handler throw, so the message's params throw on their first read.
  let reads = 0;
  const msg = {
    jsonrpc: '2.0',
    id: 1,
    method: 'prompts/get',
    get params() {
      if (reads++ === 0) {
        throw new Error('down');
      }
      return {};
    },
  };
  const request = new Request('https://publicdata.au/mcp', {
    method: 'POST',
    headers: { 'user-agent': AGENT },
  });
  request.json = async () => msg;
  const { lines } = await logged(t, () =>
    assert.rejects(onRequestPost({ request, env: {}, waitUntil() {} }), /down/),
  );
  assert.deepEqual(
    [lines[0].status, lines[0].errors, lines[0].methods],
    [500, { internal: 1 }, { 'prompts/get': 1 }],
  );
});

test('a log that fails does not fail the answer', async (t) => {
  t.mock.method(console, 'log', () => {
    throw new Error('the log is down');
  });
  const res = await post(rpc(7, 'ping'));
  assert.equal(res.status, 200);
  assert.deepEqual((await res.json()).result, {});
  assert.equal(onRequestGet().status, 405);
});
