import { json } from '../../_lib.js';
import { policy, spend } from '../../_limit.js';
import { scanCatalog, searchServed } from '../../_served.js';

// The datasets served here that match some words: search_datasets as a URL.
export async function onRequestGet({ request, env }) {
  const q = new URL(request.url).searchParams.get('q') || '';
  const wait = await spend(request, 1);
  if (wait)
    return json({ error: `Too many requests from this address. Wait ${wait} seconds.` }, 429, {
      'retry-after': String(wait),
      'ratelimit-policy': policy(),
    });
  let results = await searchServed(env, q);
  if (!results) {
    const r = await env.ASSETS.fetch(new Request('https://publicdata.au/catalog.json'));
    if (!r.ok) return json({ error: 'The dataset list could not be read' }, 503);
    results = scanCatalog(await r.json(), q);
  }
  return json({ results }, 200, {
    'cache-control': 'public, max-age=300',
    'ratelimit-policy': policy(),
  });
}
