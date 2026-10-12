import { SLUG, json, salted, today, voter } from '../../_lib.js';
import { MAX_BYTES, PER_DAY, canonical, problem, viewId } from '../../_views.js';

// The versions the explorer can open, as the build wrote them: a dataset without an explorer
// page has no list, so a dashboard is never saved where no page could show it.
async function versionsOf(env, request, slug) {
  if (!SLUG.test(slug)) {
    return null;
  }
  const r = await env.ASSETS.fetch(new URL(`/d/${slug}/explore/versions.json`, request.url));
  if (!r.ok) {
    return null;
  }
  const v = await r.json();
  return Array.isArray(v.versions) ? v.versions : null;
}

// Save an explorer dashboard and get its short id.
export async function onRequestPost({ request, env }) {
  if (!salted(env)) {
    return json(
      {
        error:
          'Dashboards cannot be saved at the moment. The link in the address bar works without saving.',
      },
      503,
    );
  }
  const text = await request.text();
  if (new TextEncoder().encode(text).length > MAX_BYTES) {
    return json({ error: `A dashboard is at most ${MAX_BYTES} bytes` }, 413);
  }
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    return json({ error: 'Send JSON with slug, version and workspace' }, 400);
  }
  const err = problem(
    body,
    body && typeof body.slug === 'string' ? await versionsOf(env, request, body.slug) : null,
  );
  if (err) {
    return json({ error: err }, 400);
  }
  const record = canonical(body);
  const id = await viewId(record);
  const key = `dash/${id}.json`;
  if (!(await env.VOTES.head(key))) {
    // No dataset slug can match _dash, so this hash never equals a vote's.
    const who = await voter(request, env, '_dash');
    const mine = await env.VOTES.list({ prefix: `dq/${today()}/${who}/`, limit: PER_DAY + 1 });
    if (mine.objects.length >= PER_DAY) {
      return json(
        {
          error: `${PER_DAY} saved dashboards a day is the limit. The link in the address bar works without saving.`,
        },
        429,
      );
    }
    await env.VOTES.put(key, record, {
      httpMetadata: { contentType: 'application/json' },
      customMetadata: { saved: new Date().toISOString() },
    });
    await env.VOTES.put(`dq/${today()}/${who}/${id}`, '');
  }
  return json(
    {
      id,
      url: `https://publicdata.au/d/${body.slug}/explore/?view=${id}`,
      embed: `https://publicdata.au/d/${body.slug}/embed/?view=${id}`,
    },
    201,
  );
}
