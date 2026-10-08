import SPEC from './_tools.json' with { type: 'json' };
import { EXEC, ToolError } from './_tools.js';
import { onRequestGet as dFile } from './d/[[path]].js';
import { SLUG } from './_lib.js';
import { QueryError } from './_query.js';
import { spend } from './_limit.js';

// A remote MCP server over Streamable HTTP. It keeps no session, so each POST is one JSON-RPC
// message answered with one JSON body, and there is no event stream to GET.
const VERSIONS = SPEC.protocol_versions;
const HEADERS = {
  'access-control-allow-origin': '*',
  'access-control-allow-headers':
    'content-type, accept, mcp-protocol-version, mcp-session-id, authorization',
  'access-control-allow-methods': 'POST, GET, OPTIONS',
  'access-control-expose-headers': 'mcp-protocol-version, link',
  'cache-control': 'no-store',
  link: '<https://publicdata.au/terms/>; rel="terms-of-service"',
};
const TOOLS = SPEC.tools.map(
  ({ name, title, description, inputSchema, outputSchema, annotations }) => ({
    name,
    title,
    description,
    inputSchema,
    outputSchema,
    annotations,
  }),
);
const PROMPTS = new Map(SPEC.prompts.map((p) => [p.name, p]));
const BY_NAME = new Map(TOOLS.map((t) => [t.name, t]));
const COST = new Map(SPEC.tools.map((t) => [t.name, t.queries]));

const send = (body, status = 200) =>
  new Response(body === null ? null : JSON.stringify(body), {
    status,
    headers: {
      ...HEADERS,
      ...(body === null ? {} : { 'content-type': 'application/json; charset=utf-8' }),
    },
  });
const ok = (id, result) => ({ jsonrpc: '2.0', id, result });
const fail = (id, code, message) => ({ jsonrpc: '2.0', id: id ?? null, error: { code, message } });

async function call(ctx, params) {
  const name = params && params.name;
  const tool = BY_NAME.get(name);
  if (!tool) {
    return { error: [-32602, `no tool ${name}; tools are ${[...BY_NAME.keys()].join(', ')}`] };
  }
  const input = params.arguments && typeof params.arguments === 'object' ? params.arguments : {};
  const missing = tool.inputSchema.required.filter(
    (k) => input[k] === undefined || input[k] === '',
  );
  const unknown = Object.keys(input).filter((k) => !(k in tool.inputSchema.properties));
  const problem = missing.length
    ? `${missing.join(', ')} ${missing.length > 1 ? 'are' : 'is'} required`
    : unknown.length
      ? `${name} takes ${Object.keys(tool.inputSchema.properties).join(', ')}, not ${unknown.join(', ')}`
      : null;
  if (problem) {
    return { result: { content: [{ type: 'text', text: problem }], isError: true } };
  }
  const wait = await spend(ctx.request, COST.get(name));
  if (wait) {
    return {
      result: {
        content: [{ type: 'text', text: `rate limited; wait ${wait} seconds and call again` }],
        isError: true,
      },
    };
  }
  try {
    const out = await EXEC[name](ctx, input);
    return {
      result: { content: [{ type: 'text', text: out }], structuredContent: JSON.parse(out) },
    };
  } catch (e) {
    // A tool that fails tells the model why, so it can change the call and try again.
    if (e instanceof ToolError || e instanceof QueryError) {
      return { result: { content: [{ type: 'text', text: e.message }], isError: true } };
    }
    throw e;
  }
}

// Each queryable dataset's fields, as the build wrote them. The list is read once per isolate.
const FIELDS = new RegExp(`^https://publicdata\\.au/d/${SLUG.source.slice(1, -1)}/fields\\.json$`);
let listed;
async function resources(ctx) {
  if (!listed) {
    const r = await ctx.env.ASSETS.fetch(new Request('https://publicdata.au/mcp/resources.json'));
    if (!r.ok) {
      throw new Error(`${r.status} for /mcp/resources.json`);
    }
    listed = (await r.json()).resources;
  }
  return listed;
}

async function read(ctx, params) {
  const uri = params && params.uri;
  if (typeof uri !== 'string' || !FIELDS.test(uri)) {
    return {
      error: [
        -32602,
        `no resource ${uri}; resources are https://publicdata.au/d/<slug>/fields.json`,
      ],
    };
  }
  // Through the /d/ handler, so a file moved to R2 reads as the public URL does.
  const r = await dFile({ request: new Request(uri), env: ctx.env });
  if (!r.ok) {
    return {
      error: [-32002, `no resource ${uri}; resources/list names every dataset that has one`],
    };
  }
  return { result: { contents: [{ uri, mimeType: 'application/json', text: await r.text() }] } };
}

// A prompt's text with its arguments filled; ask_dataset attaches the dataset's fields.
async function prompt(ctx, params) {
  const p = PROMPTS.get(params && params.name);
  if (!p) {
    return {
      error: [
        -32602,
        `no prompt ${params && params.name}; prompts are ${[...PROMPTS.keys()].join(', ')}`,
      ],
    };
  }
  const args = params.arguments && typeof params.arguments === 'object' ? params.arguments : {};
  const missing = p.arguments
    .filter((a) => a.required && (args[a.name] === undefined || args[a.name] === ''))
    .map((a) => a.name);
  if (missing.length) {
    return {
      error: [-32602, `${missing.join(', ')} ${missing.length > 1 ? 'are' : 'is'} required`],
    };
  }
  const fill = (s) =>
    s.replace(/\{(\w+)\}/g, (m, k) => (args[k] === undefined ? m : String(args[k])));
  const text = fill(p.text) + (p.question && args.question ? ' ' + fill(p.question) : '');
  const messages = [{ role: 'user', content: { type: 'text', text } }];
  const takesSlug = p.arguments.some((a) => a.name === 'slug');
  if (takesSlug && args.slug) {
    const r = await read(ctx, {
      uri: SPEC.resource_template.uriTemplate.replace('{slug}', args.slug),
    });
    if (r.error) {
      return { error: [-32602, `no dataset ${args.slug}; find one with search_datasets`] };
    }
    messages.push({ role: 'user', content: { type: 'resource', resource: r.result.contents[0] } });
  }
  return { result: { description: p.description, messages } };
}

// Suggests slugs for the fields template and for prompt arguments named slug.
async function complete(ctx, params) {
  const ref = (params && params.ref) || {};
  const arg = (params && params.argument) || {};
  const known =
    ref.type === 'ref/resource'
      ? ref.uri === SPEC.resource_template.uriTemplate
      : ref.type === 'ref/prompt' &&
        PROMPTS.has(ref.name) &&
        PROMPTS.get(ref.name).arguments.some((a) => a.name === 'slug');
  if (!known || arg.name !== 'slug') {
    return { values: [], hasMore: false };
  }
  const v = String(arg.value || '').toLowerCase();
  const all = (await resources(ctx)).map((r) => r.name).filter((s) => s.includes(v));
  return { values: all.slice(0, 100), total: all.length, hasMore: all.length > 100 };
}

async function handle(ctx, msg) {
  if (!msg || typeof msg !== 'object' || msg.jsonrpc !== '2.0' || typeof msg.method !== 'string') {
    return fail(msg && msg.id, -32600, 'not a JSON-RPC 2.0 request');
  }
  const { id, method, params } = msg;
  // A notification has no id and gets no answer.
  if (id === undefined) {
    return null;
  }
  if (method === 'initialize') {
    const asked = params && params.protocolVersion;
    return ok(id, {
      protocolVersion: VERSIONS.includes(asked) ? asked : VERSIONS[0],
      capabilities: {
        tools: { listChanged: false },
        resources: { listChanged: false, subscribe: false },
        prompts: { listChanged: false },
        completions: {},
      },
      serverInfo: SPEC.server,
      instructions: SPEC.instructions,
    });
  }
  if (method === 'ping') {
    return ok(id, {});
  }
  if (method === 'tools/list') {
    return ok(id, { tools: TOOLS });
  }
  if (method === 'resources/list') {
    try {
      return ok(id, { resources: await resources(ctx) });
    } catch (e) {
      return fail(
        id,
        -32603,
        'the resource list could not be read: ' + String((e && e.message) || e).slice(0, 200),
      );
    }
  }
  if (method === 'prompts/list') {
    return ok(id, {
      prompts: SPEC.prompts.map(({ name, title, description, arguments: a }) => ({
        name,
        title,
        description,
        arguments: a,
      })),
    });
  }
  if (method === 'prompts/get') {
    const r = await prompt(ctx, params);
    return r.error ? fail(id, ...r.error) : ok(id, r.result);
  }
  if (method === 'completion/complete') {
    try {
      return ok(id, { completion: await complete(ctx, params) });
    } catch (e) {
      return fail(
        id,
        -32603,
        'no completions could be read: ' + String((e && e.message) || e).slice(0, 200),
      );
    }
  }
  if (method === 'resources/templates/list') {
    return ok(id, { resourceTemplates: [SPEC.resource_template] });
  }
  if (method === 'resources/read') {
    const r = await read(ctx, params);
    return r.error ? fail(id, ...r.error) : ok(id, r.result);
  }
  if (method === 'tools/call') {
    let r;
    try {
      r = await call(ctx, params);
    } catch (e) {
      return fail(
        id,
        -32603,
        'the tool could not run: ' + String((e && e.message) || e).slice(0, 200),
      );
    }
    return r.error ? fail(id, ...r.error) : ok(id, r.result);
  }
  return fail(
    id,
    -32601,
    `no method ${method}; this server has tools, resources, prompts and completions`,
  );
}

export async function onRequestPost(context) {
  const v = context.request.headers.get('mcp-protocol-version');
  if (v && !VERSIONS.includes(v)) {
    return send(
      fail(
        null,
        -32600,
        `unsupported MCP-Protocol-Version ${v}; supported are ${VERSIONS.join(', ')}`,
      ),
      400,
    );
  }
  let msg;
  try {
    msg = await context.request.json();
  } catch {
    return send(fail(null, -32700, 'the body is not JSON'), 400);
  }
  const ctx = {
    env: context.env,
    request: context.request,
    waitUntil: (p) => context.waitUntil(p),
  };
  if (Array.isArray(msg)) {
    // One at a time, so a batch cannot race its own rate count.
    const out = [];
    for (const m of msg) {
      const r = await handle(ctx, m);
      if (r) {
        out.push(r);
      }
    }
    return out.length ? send(out) : send(null, 202);
  }
  const out = await handle(ctx, msg);
  return out ? send(out) : send(null, 202);
}

export const onRequestOptions = () => new Response(null, { status: 204, headers: HEADERS });

export const onRequestGet = () =>
  new Response(
    JSON.stringify({
      error:
        'This MCP server answers POST only. Connect an MCP client to https://publicdata.au/mcp.',
      docs: 'https://publicdata.au/agents/#mcp',
    }),
    {
      status: 405,
      headers: {
        ...HEADERS,
        allow: 'POST, OPTIONS',
        'content-type': 'application/json; charset=utf-8',
      },
    },
  );
