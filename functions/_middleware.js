// _routes.json sends every .md path here as well as the paths the functions under functions/
// answer. Those paths pass straight on. A .md file is served by Pages as it is, and a guess at a
// page's twin that Pages has no file for is sent to the twin.

// ROUTES in pipeline/publicdata/site.py, which a test holds this list to.
export const FUNCTION_ROUTES = [
  '/api/*',
  '/mcp',
  '/d/*/latest/*',
  '/d/*/fetch/*',
  '/d/*/v/*',
  '/d/*/diff/*',
  '/d/*/history.tar.zst',
  '/d/*/changes/*',
];

// A rule as Pages reads it: a * crosses slashes, and a rule also matches its folder without a slash.
export function routeRule(rule) {
  let r = rule;
  if (r !== '/' && r !== '/*') {
    if (r.endsWith('/*')) {
      r = r.slice(0, -2) + '(/*)?';
    } else if (r.endsWith('/')) {
      r = r.slice(0, -1) + '(/)?';
    } else if (!r.endsWith('*')) {
      r += '(/)?';
    }
  }
  return new RegExp('^' + r.replaceAll('.', '\\.').replaceAll('*', '.*') + '$');
}

const OWNED = FUNCTION_ROUTES.map(routeRule);

const isTwin = (r) => r.ok && (r.headers.get('content-type') || '').startsWith('text/markdown');

// Every published path is plain. A path the URL parser could read as another host, such as //x or
// /\x, or one with an escaped slash, is served by Pages as it is and never built on.
const plain = (p) => !p.startsWith('//') && !/\\|%2f|%5c/i.test(p);

// /x.md and /x/index.html.md are guesses at /x/index.md, sent there when it exists.
async function guessed(request, env, url) {
  const r = await env.ASSETS.fetch(request);
  const p = url.pathname;
  const base = p.slice(0, -'.md'.length);
  if (r.status !== 404 || p.endsWith('/index.md') || base.endsWith('/')) {
    return r;
  }
  // Concatenated onto the origin, never resolved against the request, and sent as a path.
  const to = new URL(
    url.origin +
      (base.endsWith('/index.html')
        ? base.slice(0, -'index.html'.length) + 'index.md'
        : base + '/index.md'),
  );
  if (to.origin !== url.origin || to.pathname.startsWith('//')) {
    return r;
  }
  const t = await env.ASSETS.fetch(new Request(to, { method: 'HEAD' }));
  if (!isTwin(t)) {
    return r;
  }
  if (r.body) {
    await r.body.cancel();
  }
  return new Response(null, {
    status: 308,
    headers: { location: to.pathname + url.search, 'cache-control': 'public, max-age=86400' },
  });
}

export async function onRequest({ request, env, next, passThroughOnException }) {
  const url = new URL(request.url);
  if (OWNED.some((r) => r.test(url.pathname))) {
    return next();
  }
  // Only here, past the function routes, whose own errors must stay errors.
  passThroughOnException();
  if (
    (request.method === 'GET' || request.method === 'HEAD') &&
    url.pathname.endsWith('.md') &&
    plain(url.pathname)
  ) {
    return guessed(request, env, url);
  }
  return env.ASSETS.fetch(request);
}
