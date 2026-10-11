import assert from 'node:assert/strict';
import { test } from 'node:test';
import { line } from '../functions/_usage.js';
import { count, report } from './mcp-usage.mjs';

const req = (ua, method = 'POST') =>
  new Request('https://publicdata.au/mcp', { method, headers: ua ? { 'user-agent': ua } : {} });
const rpc = (method, params) => ({ jsonrpc: '2.0', id: 1, method, params });
const log = (...a) => JSON.stringify(line(...a));

// An event as `wrangler pages deployment tail --format json` prints it: pretty, back to back.
const tail = (message) =>
  JSON.stringify(
    {
      outcome: 'ok',
      scriptName: 'pages-worker--1-production',
      logs: [{ message: [message], level: 'log', timestamp: 1 }],
      event: { request: { url: 'https://publicdata.au/mcp', method: 'POST', headers: {} } },
    },
    null,
    4,
  );
// The same in a Logpush file of Workers trace events, one event a line.
const pushed = (message) =>
  JSON.stringify({ Outcome: 'ok', Logs: [{ Level: 'log', Message: [message], TimestampMs: 1 }] });

test('counts usage lines from a tail and a Logpush file, split client and probe', () => {
  const text = [
    tail(
      log(
        req('claude-code/2.1'),
        rpc('initialize', { clientInfo: { name: 'claude-code', version: '2.1' } }),
        null,
        200,
      ),
    ),
    tail(
      log(
        req('claude-code/2.1'),
        rpc('tools/call', { name: 'query_rows', arguments: { slug: 1 } }),
        null,
        200,
      ),
    ),
    tail('an error line that is not ours {"event":"mcp"'),
    pushed(log(req('node'), rpc('tools/call', { name: 'query_rows', arguments: {} }), null, 200)),
    pushed(log(req('node'), rpc('tools/call', { name: 'get_dataset', arguments: {} }), null, 200)),
    pushed(log(req(''), rpc('initialize', {}), null, 200)),
    pushed(log(req('curl/8', 'GET'), undefined, null, 405)),
    '{"outcome":"ok","logs":[{"message":["cut off',
  ].join('\n');
  const c = count(text);
  assert.deepEqual(c.client.tool, { query_rows: 2, get_dataset: 1 });
  assert.deepEqual(c.client['tool call by agent'], { node: 2, 'claude-code/2.1': 1 });
  assert.deepEqual(c.client['initialize by client'], { 'claude-code 2.1': 1 });
  assert.deepEqual(c.probe.method, { initialize: 1, 'GET 405': 1 });
  assert.deepEqual(c.probe['initialize by client'], { '(none)': 1 });
  const r = report(c);
  assert.match(r, /^client\n {2}method\n {4} {5}3 {2}tools\/call\n/);
  assert.ok(r.indexOf('probe') > r.indexOf('query_rows'));
});
