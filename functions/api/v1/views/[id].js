import { json } from '../../../_lib.js';
import { ID } from '../../../_views.js';

// A saved dashboard. Its id is the hash of its content, so the answer is cached for good.
export async function onRequestGet({ env, params }) {
  if (!ID.test(params.id)) {
    return json({ error: 'No such dashboard' }, 404);
  }
  const o = await env.VOTES.get(`dash/${params.id}.json`);
  if (!o) {
    return json({ error: 'No such dashboard' }, 404);
  }
  return new Response(o.body, {
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'public, max-age=31536000, immutable',
      'access-control-allow-origin': '*',
    },
  });
}
