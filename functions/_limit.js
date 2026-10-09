import SPEC from './_tools.json' with { type: 'json' };

// The zone's limit matches /api/v1/datasets/* only. The MCP server, the catalogue search and the
// link lookup reach D1 without passing it, so they keep the same limit here, counted per address
// in each edge location's cache. Returns the seconds to wait, or 0.
export async function spend(request, cost) {
  if (!cost) {
    return 0;
  }
  const { requests, window_seconds: w } = SPEC.limits;
  const now = Math.floor(Date.now() / 1000);
  const ip = request.headers.get('cf-connecting-ip') || '0.0.0.0';
  const key = new Request(
    `https://publicdata.au/_limit/${encodeURIComponent(ip)}/${Math.floor(now / w)}`,
  );
  const hit = await caches.default.match(key);
  const used = hit ? Number(await hit.text()) || 0 : 0;
  if (used + cost > requests) {
    return w - (now % w);
  }
  await caches.default.put(
    key,
    new Response(String(used + cost), { headers: { 'cache-control': `max-age=${w}` } }),
  );
  return 0;
}

export const policy = () => `"fair-use";q=${SPEC.limits.requests};w=${SPEC.limits.window_seconds}`;
