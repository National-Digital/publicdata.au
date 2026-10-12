import assert from 'node:assert/strict';
import { test } from 'node:test';
import { line } from '../functions/_usage.js';
import { count, report } from './mcp-usage.mjs';

const req = (ua, method = 'POST', headers = {}) =>
  new Request('https://publicdata.au/mcp', {
    method,
    headers: ua ? { 'user-agent': ua, ...headers } : headers,
  });
const rpc = (method, params) => ({ jsonrpc: '2.0', id: 1, method, params });
const log = (r, msgs, status = 200) =>
  JSON.stringify(
    line(
      r,
      msgs.map((m) => [m, null]),
      status,
    ),
  );

// An event as `wrangler pages deployment tail --format json` prints it: pretty, back to back,
// with the request's headers beside the console lines.
const tail = (message, headers = {}) =>
  JSON.stringify(
    {
      outcome: 'ok',
      logs: [{ message: [message], level: 'log', timestamp: 1 }],
      event: { request: { url: 'https://publicdata.au/mcp', method: 'POST', headers } },
    },
    null,
    4,
  );
// The same in a Logpush file of Workers trace events, one event a line.
const pushed = (message) =>
  JSON.stringify({ Outcome: 'ok', Logs: [{ Level: 'log', Message: [message], TimestampMs: 1 }] });

const forged = log(req('node'), [rpc('tools/call', { name: 'upvote_dataset' })]);

test('counts usage lines from a tail and a Logpush file, split client and probe', () => {
  const text = [
    tail(
      log(req('claude-code/2.1'), [
        rpc('initialize', { clientInfo: { name: 'claude-code', version: '2.1.0' } }),
      ]),
    ),
    // Cut off by Ctrl-C part way through.
    tail('x').split('\n').slice(0, 4).join('\n'),
    tail(log(req('claude-code/2.1'), [rpc('tools/call', { name: 'query_rows' })]), {
      'x-forged': forged,
    }),
    tail('an error line that is not ours {"event":"mcp"'),
    pushed(
      log(req('node'), [
        rpc('tools/call', { name: 'query_rows' }),
        rpc('tools/call', { name: 'get_dataset' }),
      ]),
    ),
    pushed(log(req(''), [rpc('initialize', {})])),
    pushed(log(req('curl/8', 'GET'), [], 405)),
    'not json',
  ].join('\n');
  const { counts: c, skipped } = count(text);
  assert.equal(skipped, 2);
  assert.deepEqual(c.client.tool, { query_rows: 2, get_dataset: 1 });
  assert.equal(c.client.tool.upvote_dataset, undefined, 'a header is never read as a line');
  assert.deepEqual(c.client['tool call by agent'], { node: 2, 'claude-code': 1 });
  assert.deepEqual(c.client['initialize by client'], { 'claude-code 2.1.0': 1 });
  assert.deepEqual(c.probe.method, { initialize: 1 });
  assert.deepEqual(c.probe.request, { 'POST 200': 1, 'GET 405': 1 });
  const r = report({ counts: c, skipped });
  assert.match(r, /^client\n {2}request\n {4} {5}3 {2}POST 200\n/);
  assert.match(r, /skipped 2 inputs that were not whole JSON$/);
});
