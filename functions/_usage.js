import SPEC from './_tools.json' with { type: 'json' };

// One line per MCP request in the Functions log, so a maintainer can count tools and clients.
// Every string written comes from a list in this file or in _tools.json, and anything else the
// caller sent becomes "other": arguments are reduced to the names a tool or prompt declares, and
// the address, the session id and the values of the arguments are never written.
const METHODS = new Set([
  'initialize',
  'ping',
  'tools/list',
  'tools/call',
  'resources/list',
  'resources/read',
  'resources/templates/list',
  'prompts/list',
  'prompts/get',
  'completion/complete',
  'notifications/initialized',
  'notifications/cancelled',
]);
const TOOL_ARGS = new Map(SPEC.tools.map((t) => [t.name, Object.keys(t.inputSchema.properties)]));
const PROMPT_ARGS = new Map(SPEC.prompts.map((p) => [p.name, p.arguments.map((a) => a.name)]));

// clientInfo names read from each client's own source or shipped code.
const CLIENTS = new Set([
  'claude-code',
  'mcp-inspector',
  'mcp',
  'mcp-remote',
  'codex-mcp-client',
  'gemini-cli-mcp-client',
  'Visual Studio Code',
  'Visual Studio Code - Insiders',
  'Code - OSS',
]);
// mcp-remote passes the client's own name on with this suffix.
const VIA = /^(.*) \(via mcp-remote [^()]*\)$/s;
const VERSION =
  /^v?((?:0|[1-9]\d{0,3})\.(?:0|[1-9]\d{0,3})\.(?:0|[1-9]\d{0,3}))(?:[-+][0-9A-Za-z.-]*)?$/;

// The product names written from a User-Agent; Mozilla only when the agent names nothing else.
const AGENTS = new Map(
  [
    'claude-code',
    'claude-cli',
    'Claude-User',
    'ChatGPT-User',
    'node',
    'undici',
    'Deno',
    'Bun',
    'axios',
    'node-fetch',
    'python-httpx',
    'python-requests',
    'aiohttp',
    'Go-http-client',
    'okhttp',
    'Java',
    'reqwest',
    'curl',
    'Wget',
    'HTTPie',
    'Mozilla',
  ].map((a) => [a.toLowerCase(), a]),
);
// A word of the agent that names a crawler, scanner or command-line fetcher marks a probe.
const PROBE =
  /bot$|bot-|crawl|spider|scan|zgrab|masscan|nmap|censys|shodan|^curl$|^wget$|^httpie$/i;

const words = (ua) =>
  ua
    .split(/[\s;(),]+/)
    .map((w) => w.split('/')[0])
    .filter(Boolean);

function agent(ua) {
  if (!ua) {
    return null;
  }
  const known = words(ua)
    .map((w) => AGENTS.get(w.toLowerCase()))
    .filter(Boolean);
  return known.find((a) => a !== 'Mozilla') || known[0] || 'other';
}

function clientOf(info) {
  if (!info || typeof info.name !== 'string') {
    return { known: null, raw: null };
  }
  const via = VIA.exec(info.name);
  const base = via ? via[1] : info.name;
  const r = { known: CLIENTS.has(base) ? base : 'other', raw: base };
  if (via) {
    r.proxy = 'mcp-remote';
  }
  const v = r.known !== 'other' && typeof info.version === 'string' && VERSION.exec(info.version);
  if (v) {
    r.version = v[1];
  }
  return r;
}

const bump = (o, k) => {
  o[k] = (o[k] || 0) + 1;
};

// calls are [message, answer] pairs, the answer null for a notification or a call that threw.
// error names a failure of the request as a whole: protocol, json or internal.
export function line(request, calls, status, error, batch) {
  const ua = (request.headers.get('user-agent') || '').trim();
  const v = request.headers.get('mcp-protocol-version');
  const r = {
    event: 'mcp',
    http: request.method,
    status,
    ua: agent(ua),
    protocol: v ? (SPEC.protocol_versions.includes(v) ? v : 'other') : null,
    session: request.headers.has('mcp-session-id'),
    batch: Boolean(batch),
    calls: calls.length,
    methods: {},
  };
  const errors = {};
  if (error) {
    bump(errors, error);
  }
  let valid = 0;
  let probe = request.method !== 'POST' || error === 'json' || words(ua).some((w) => PROBE.test(w));
  for (const [msg, out] of calls) {
    const ok = msg && typeof msg === 'object' && msg.jsonrpc === '2.0';
    const method = ok && typeof msg.method === 'string' ? msg.method : null;
    if (!method) {
      bump(errors, 'invalid');
      continue;
    }
    valid++;
    const m = METHODS.has(method) ? method : 'other';
    bump(r.methods, m);
    const params = msg.params && typeof msg.params === 'object' ? msg.params : {};
    if (m === 'tools/call' || m === 'prompts/get') {
      const lists = m === 'tools/call' ? TOOL_ARGS : PROMPT_ARGS;
      const kind = m === 'tools/call' ? 'tools' : 'prompts';
      const names = lists.get(params.name);
      const name = names ? params.name : 'other';
      r[kind] ||= {};
      bump(r[kind], name);
      const given = params.arguments;
      if (names && given && typeof given === 'object' && !Array.isArray(given)) {
        const keys = Object.keys(given);
        const seen = new Set([
          ...((r.args ||= {})[name] || []),
          ...names.filter((k) => keys.includes(k)),
        ]);
        if (keys.some((k) => !names.includes(k))) {
          seen.add('other');
        }
        r.args[name] = [...seen].sort();
      }
    }
    if (m === 'initialize') {
      const c = clientOf(params.clientInfo);
      // The first initialize names the client, so its name, version and proxy stay together.
      if (!('client' in r)) {
        r.client = c.known;
        if (c.version) {
          r.client_version = c.version;
        }
        if (c.proxy) {
          r.proxy = c.proxy;
        }
      }
      probe ||= !c.raw || PROBE.test(c.raw);
    }
    if (out && out.error) {
      bump(errors, String(out.error.code));
    } else if (out && out.result && out.result.isError) {
      bump(errors, 'tool');
    }
  }
  if (Object.keys(errors).length) {
    r.errors = errors;
  }
  // A request with a body that holds no JSON-RPC call at all is not a client's.
  r.class =
    probe || (calls.length === 0 && error !== 'protocol') || (calls.length && !valid)
      ? 'probe'
      : 'client';
  return r;
}

// Never throws, and writes nothing a caller can lengthen: every list in the line is bounded.
export function usage(request, calls, status, error, batch) {
  try {
    // eslint-disable-next-line no-console -- the Functions log is where the counts are read.
    console.log(JSON.stringify(line(request, calls, status, error, batch)));
  } catch {
    // A count is not worth a failed answer.
  }
}
