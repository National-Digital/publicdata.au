import SPEC from './_tools.json' with { type: 'json' };

// One line per MCP call in the Functions log, so a maintainer can count tools and clients.
// Nothing the caller typed is written: a name outside the server's own lists becomes "other",
// arguments are reduced to the names the tool or prompt declares, and the address, the session
// id and every other header but the user agent's first word are left out.
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

// Only an agent that names itself as a crawler, scanner or command-line fetcher counts as a
// probe; an MCP SDK's own agent (node, python-httpx and the like) counts as a client.
const PROBE_AGENT =
  /bot\b|bot\/|crawl|spider|scan|zgrab|masscan|nuclei|censys|shodan|nmap|^curl\/|^wget\/|^httpie\//i;

// A short label from a caller's string: printable, at most 64 characters, never an email address.
function label(s) {
  if (typeof s !== 'string') {
    return null;
  }
  const t = s
    .replace(/[^\w .:/+@-]/g, '')
    .trim()
    .slice(0, 64);
  return !t || /\S@\S+\.\S/.test(t) ? null : t;
}

const declared = (given, names) => {
  if (!given || typeof given !== 'object' || Array.isArray(given)) {
    return [];
  }
  const keys = Object.keys(given);
  const known = names.filter((k) => keys.includes(k)).sort();
  return keys.some((k) => !names.includes(k)) ? [...known, 'other'] : known;
};

function rpcOf(msg) {
  const ok = msg && typeof msg === 'object' && msg.jsonrpc === '2.0';
  const method = ok && typeof msg.method === 'string' ? msg.method : null;
  return { ok: Boolean(method), method: method && (METHODS.has(method) ? method : 'other') };
}

export function line(request, msg, out, status, batch) {
  const ua = (request.headers.get('user-agent') || '').trim();
  const v = request.headers.get('mcp-protocol-version');
  const { ok, method } = rpcOf(msg);
  const params = (msg && msg.params) || {};
  const r = {
    event: 'mcp',
    http: request.method,
    status,
    rpc: method,
    ua: label(ua.split(/\s/)[0]),
    protocol: v ? (SPEC.protocol_versions.includes(v) ? v : 'other') : null,
    session: request.headers.has('mcp-session-id'),
  };
  if (batch) {
    r.batch = batch;
  }
  if (method === 'tools/call') {
    const names = TOOL_ARGS.get(params.name);
    r.tool = names ? params.name : 'other';
    r.args = names ? declared(params.arguments, names) : [];
  }
  if (method === 'prompts/get') {
    const names = PROMPT_ARGS.get(params.name);
    r.prompt = names ? params.name : 'other';
    r.args = names ? declared(params.arguments, names) : [];
  }
  if (method === 'initialize') {
    const info = params.clientInfo || {};
    r.client = label(info.name);
    r.client_version = label(info.version);
  }
  if (out && out.error) {
    r.error = out.error.code;
  } else if (out && out.result && out.result.isError) {
    r.error = 'tool';
  }
  r.class =
    request.method !== 'POST' ||
    status >= 400 ||
    !ok ||
    !ua ||
    PROBE_AGENT.test(ua) ||
    (method === 'initialize' && (!r.client || PROBE_AGENT.test(r.client)))
      ? 'probe'
      : 'client';
  return r;
}

// Each call is [message, answer]; the answer is null for a notification. Never throws.
export function usage(request, calls, status, batch) {
  try {
    for (const [msg, out] of calls.length ? calls : [[undefined, null]]) {
      // eslint-disable-next-line no-console -- the Functions log is where the counts are read.
      console.log(JSON.stringify(line(request, msg, out, status, batch)));
    }
  } catch {
    // A count is not worth a failed answer.
  }
}
