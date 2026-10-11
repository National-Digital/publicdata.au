import assert from 'node:assert/strict';
import { test } from 'node:test';
import { onRequestGet, onRequestHead, onRequestPost } from './mcp.js';
import { line } from './_usage.js';

// The usage line for each MCP call, read from console.log as the Functions log would read it.
// Only calls that answer before any data is read are made, so no D1 or asset stand-in is needed.
const AGENT = 'claude-code/2.1.0 (cli)';
const post = (body, headers = {}) =>
  onRequestPost({
    request: new Request('https://publicdata.au/mcp', {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'user-agent': AGENT, ...headers },
      body: typeof body === 'string' ? body : JSON.stringify(body),
    }),
    env: {},
    waitUntil() {},
  });
const rpc = (id, method, params) => ({ jsonrpc: '2.0', id, method, params });

async function logged(t, run) {
  const seen = [];
  t.mock.method(console, 'log', (s) => seen.push(s));
  const res = await run();
  return { res, lines: seen.map((s) => JSON.parse(s)), raw: seen.join('\n') };
}

test('initialize records the client name and version, and later calls the agent', async (t) => {
  const { res, lines } = await logged(t, () =>
    post(
      rpc(1, 'initialize', {
        protocolVersion: '2025-06-18',
        clientInfo: { name: 'claude-code', version: '2.1.0' },
      }),
    ),
  );
  assert.equal(res.status, 200);
  assert.deepEqual(lines, [
    {
      event: 'mcp',
      http: 'POST',
      status: 200,
      rpc: 'initialize',
      ua: 'claude-code/2.1.0',
      protocol: null,
      session: false,
      client: 'claude-code',
      client_version: '2.1.0',
      class: 'client',
    },
  ]);
  const later = await logged(t, () =>
    post(rpc(2, 'tools/list'), {
      'mcp-protocol-version': '2025-06-18',
      'mcp-session-id': 'abc123',
    }),
  );
  const [l] = later.lines;
  assert.equal(l.rpc, 'tools/list');
  assert.equal(l.protocol, '2025-06-18');
  assert.equal(l.session, true);
  assert.equal(l.class, 'client');
  assert.ok(!later.raw.includes('abc123'), 'the session id is never written');
});

test('a tool call records the tool and its argument names, never their values', async (t) => {
  const question = 'how many crashes near my house at 12 Smith St';
  const { res, lines, raw } = await logged(t, () =>
    post(
      rpc(3, 'tools/call', {
        name: 'search_datasets',
        arguments: { query: question, [question]: question },
      }),
      { 'cf-connecting-ip': '203.0.113.9', authorization: 'Bearer s3cret' },
    ),
  );
  assert.equal((await res.json()).result.isError, true);
  assert.equal(lines[0].tool, 'search_datasets');
  assert.deepEqual(lines[0].args, ['query', 'other']);
  assert.equal(lines[0].error, 'tool');
  for (const s of ['Smith', 'crashes', '203.0.113.9', 's3cret', 'Bearer']) {
    assert.ok(!raw.includes(s), `${s} is not written`);
  }
  const other = await logged(t, () =>
    post(rpc(4, 'tools/call', { name: 'tell me about Smith St', arguments: { a: 1 } })),
  );
  assert.equal(other.lines[0].tool, 'other');
  assert.deepEqual(other.lines[0].args, []);
  assert.equal(other.lines[0].error, -32602);
  assert.ok(!other.raw.includes('Smith'));
});

test('a method or client name of the caller is kept only when it is plain', () => {
  const req = new Request('https://publicdata.au/mcp', {
    method: 'POST',
    headers: { 'user-agent': 'node' },
  });
  assert.equal(line(req, rpc(1, 'Smith St/secret'), null, 200).rpc, 'other');
  const named = (name) =>
    line(req, rpc(1, 'initialize', { clientInfo: { name, version: '1' } }), null, 200);
  assert.equal(named('jo.citizen@example.com').client, null);
  assert.equal(named('@modelcontextprotocol/inspector').client, '@modelcontextprotocol/inspector');
  assert.equal(named('x'.repeat(500)).client.length, 64);
  assert.equal(named('<script>"q"</script>').client, 'scriptq/script');
});

test('a batch writes one line per call, a notification included', async (t) => {
  const { res, lines } = await logged(t, () =>
    post([
      rpc(5, 'ping'),
      { jsonrpc: '2.0', method: 'notifications/initialized' },
      rpc(6, 'prompts/get', { name: 'find_data', arguments: { topic: 'secret', extra: 1 } }),
    ]),
  );
  assert.equal(res.status, 200);
  assert.deepEqual(
    lines.map((l) => [l.rpc, l.batch, l.class]),
    [
      ['ping', 3, 'client'],
      ['notifications/initialized', 3, 'client'],
      ['prompts/get', 3, 'client'],
    ],
  );
  assert.equal(lines[2].prompt, 'find_data');
  assert.deepEqual(lines[2].args, ['topic', 'other']);
});

test('probes are told apart from clients on evidence only', async (t) => {
  const cls = async (body, headers) => (await logged(t, () => post(body, headers))).lines[0].class;
  const init = (clientInfo) => rpc(1, 'initialize', { protocolVersion: '2025-06-18', clientInfo });
  assert.equal(await cls(init({ name: 'cursor-vscode', version: '1.0' })), 'client');
  assert.equal(await cls(init({ name: 'x', version: '1' }), { 'user-agent': 'node' }), 'client');
  assert.equal(
    await cls(init({ name: 'x', version: '1' }), { 'user-agent': 'python-httpx/0.28.1' }),
    'client',
  );
  assert.equal(await cls(rpc(1, 'logging/setLevel', { level: 'info' })), 'client');
  // No agent, a scanner's agent, no clientInfo, a scanner's clientInfo, a malformed request.
  assert.equal(await cls(init({ name: 'x', version: '1' }), { 'user-agent': '' }), 'probe');
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
  assert.equal(await cls(rpc(1, 'ping'), { 'mcp-protocol-version': '1999-01-01' }), 'probe');

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
      [lines[0].http, lines[0].rpc, lines[0].status, lines[0].class],
      [method, null, 405, 'probe'],
    );
  }
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
