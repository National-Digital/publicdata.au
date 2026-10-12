import assert from 'node:assert/strict';
import { test } from 'node:test';
import { onRequest, routeRule } from './_middleware.js';

const MD = 'text/markdown; charset=utf-8';
const FILES = {
  '/index.html': ['<html>home</html>', 'text/html; charset=utf-8'],
  '/index.md': ['# Home\n', MD],
  '/d/x/index.html': ['<html>x</html>', 'text/html; charset=utf-8'],
  '/d/x/index.md': ['---\ntitle: X\n---\n\n# X\n', MD],
  '/d/x/embed/index.html': ['<html>embed</html>', 'text/html; charset=utf-8'],
  '/skills/s/SKILL.md': ['# Skill', MD],
};

// Pages as the ASSETS binding answers: a folder's index.html, a 304 for a matching ETag, 404.html.
// Like Pages it reads a run of slashes as one. With anyTwin it finds a twin at any path, so only
// the middleware's own checks stop a redirect.
function pages({ anyTwin = false } = {}) {
  const seen = [];
  const hosts = [];
  const env = {
    ASSETS: {
      fetch: async (r) => {
        const req = r instanceof Request ? r : new Request(r);
        hosts.push(new URL(req.url).host);
        const p = new URL(req.url).pathname.replace(/\/+/g, '/');
        seen.push(`${req.method} ${p}`);
        const f =
          FILES[p.endsWith('/') ? p + 'index.html' : p] ||
          (anyTwin && p.endsWith('/index.md') ? ['# any', MD] : undefined);
        if (!f) {
          return new Response('<html>404</html>', {
            status: 404,
            headers: { 'content-type': MD },
          });
        }
        const headers = {
          'content-type': f[1],
          etag: `"${p}"`,
          'content-security-policy': "default-src 'self'",
        };
        if (req.headers.get('if-none-match') === `"${p}"`) {
          return new Response(null, { status: 304, headers });
        }
        return new Response(req.method === 'HEAD' ? null : f[0], { headers });
      },
    },
  };
  return { env, seen, hosts };
}

function call(path, { method = 'GET', headers = {}, anyTwin } = {}, next) {
  const { env, seen, hosts } = pages({ anyTwin });
  const nexts = [];
  let failOpen = 0;
  const request = new Request('https://publicdata.au' + path, { method, headers });
  const r = onRequest({
    request,
    env,
    next: async (...args) => {
      nexts.push(args);
      return next ? next() : new Response('fn');
    },
    passThroughOnException: () => {
      failOpen++;
    },
  });
  return r.then((res) => ({ res, seen, nexts, hosts, failOpen }));
}

test('/x.md and /x/index.html.md are sent to /x/index.md', async () => {
  for (const [from, to] of [
    ['/d/x.md', '/d/x/index.md'],
    ['/d/x/index.html.md', '/d/x/index.md'],
    ['/index.html.md', '/index.md'],
  ]) {
    const { res } = await call(from + '?a=1');
    assert.equal(res.status, 308, from);
    assert.equal(res.headers.get('location'), to + '?a=1');
  }
  const head = await call('/d/x.md', { method: 'HEAD' });
  assert.equal(head.res.status, 308);
});

test('a guessed spelling with nothing behind it is the 404', async () => {
  for (const p of [
    '/nope.md',
    '/d/x/embed.md',
    '/d/x/embed/index.html.md',
    '/d/.md',
    '/q/index.md',
  ]) {
    const { res } = await call(p);
    assert.equal(res.status, 404, p);
    assert.equal(res.headers.get('location'), null, p);
  }
});

test('a twin asked for directly is the Pages response itself, conditions and all', async () => {
  const twin = await call('/d/x/index.md', { headers: { accept: 'text/markdown' } });
  assert.equal(twin.res.status, 200);
  assert.equal(await twin.res.text(), FILES['/d/x/index.md'][0]);
  assert.equal(twin.res.headers.get('content-type'), MD);
  assert.equal(twin.res.headers.get('content-security-policy'), "default-src 'self'");
  assert.equal(twin.res.headers.get('vary'), null);
  assert.deepEqual(twin.seen, ['GET /d/x/index.md']);
  const fresh = await call('/d/x/index.md', { headers: { 'if-none-match': '"/d/x/index.md"' } });
  assert.equal(fresh.res.status, 304);
  const skill = await call('/skills/s/SKILL.md');
  assert.equal(await skill.res.text(), '# Skill');
});

test('a path that could name another host is never built on', async () => {
  for (const p of [
    '//evil.example/d/x.md',
    '/\\evil.example/d/x.md',
    '///evil.example/d/x.md',
    '/%2F%2Fevil.example/d/x.md',
    '/%5Cevil.example/d/x.md',
    '//evil.example/d/x/index.html.md',
  ]) {
    const { res, hosts } = await call(p, { anyTwin: true });
    assert.notEqual(res.status, 308, p);
    assert.equal(res.headers.get('location'), null, p);
    assert.deepEqual([...new Set(hosts)], ['publicdata.au'], p);
  }
});

test('a redirect names a path on this site', async () => {
  const { res } = await call('/d/x.md');
  assert.match(res.headers.get('location'), /^\/[^/\\]/);
});

test('a range on a dated file goes to its function untouched', async () => {
  const partial = new Response('PAR1', {
    status: 206,
    headers: { 'content-range': 'bytes 0-3/9', 'accept-ranges': 'bytes' },
  });
  const { res, seen, nexts } = await call(
    '/d/x/v/2026-04-24/data.parquet',
    { headers: { range: 'bytes=0-3', accept: 'text/markdown' } },
    () => partial,
  );
  assert.equal(res, partial);
  assert.equal(res.status, 206);
  assert.deepEqual(nexts, [[]]);
  assert.deepEqual(seen, []);
});

test("every function route passes on, a dated version's twin included, and nothing else does", async () => {
  for (const p of [
    '/d/x/v/2026-04-24/',
    '/d/x/v/2026-04-24/index.md',
    '/d/x/v/2026-04-24.md',
    '/d/x/v/2026-04-24/data.duckdb?edge=bypass',
    '/d/x/latest/',
    '/d/x/latest/index.md',
    '/d/x/fetch/2026-10-06/data.csv',
    '/d/x/diff/2026-04-24/',
    '/d/x/changes/index.json',
    '/d/x/history.tar.zst',
    '/api/v1/datasets',
    '/api/v1/votes/x',
    '/mcp',
  ]) {
    for (const method of ['GET', 'HEAD', 'POST']) {
      const { res, seen, nexts } = await call(p, { method });
      assert.equal(await res.text(), 'fn', `${method} ${p}`);
      assert.equal(nexts.length, 1);
      assert.deepEqual(seen, []);
    }
  }
  for (const p of ['/d/x/index.md', '/mcp.md', '/d/x/versions.md', '/apis/x.md']) {
    const { nexts } = await call(p);
    assert.equal(nexts.length, 0, p);
  }
});

test('a route rule reads as Pages reads it', () => {
  assert.ok(routeRule('/d/*/v/*').test('/d/x/v'));
  assert.ok(routeRule('/d/*/v/*').test('/d/x/v/2026-04-24/data.csv'));
  assert.ok(!routeRule('/d/*/v/*').test('/d/x/versions.json'));
  assert.ok(routeRule('/mcp').test('/mcp/'));
  assert.ok(!routeRule('/mcp').test('/mcp/server-card'));
  assert.ok(!routeRule('/mcp').test('/mcp.md'));
  assert.ok(routeRule('/*.md').test('/d/x/index.md'));
  assert.ok(routeRule('/d/*/history.tar.zst').test('/d/x/history.tar.zst'));
  assert.ok(!routeRule('/d/*/history.tar.zst').test('/d/x/historyXtarXzst'));
});

test('other methods on a .md path go to Pages as they are', async () => {
  const { res, seen } = await call('/d/x.md', { method: 'POST' });
  assert.equal(res.status, 404);
  assert.deepEqual(seen, ['POST /d/x.md']);
});

test("an error past the function routes falls back to Pages, and a function's own stays an error", async () => {
  assert.equal((await call('/d/x.md')).failOpen, 1);
  for (const p of ['/d/x/v/2026-04-24/data.csv', '/api/v1/datasets', '/mcp']) {
    assert.equal((await call(p)).failOpen, 0, p);
  }
});
