import { json } from '../../_lib.js';
import { resolve, table } from '../../_catalogue.js';
import { shape } from './catalogue.js';
import { policy, spend } from '../../_limit.js';
import { onRequestPost as castVote } from './votes/[slug].js';

export const CONTACT = 'https://nationaldigital.com.au/contact/';

// "Add a dataset": a portal URL becomes a vote for the record it names. A URL the catalogue does
// not hold is not stored; the answer points to a person instead.
export async function onRequestPost({ request, env }) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: 'Send JSON with a url' }, 400);
  }
  let url;
  try {
    url = new URL(String(body.url || ''));
  } catch {
    return json({ error: 'That is not a URL' }, 400);
  }
  if (!/^https?:$/.test(url.protocol)) {
    return json({ error: 'The URL must start with http or https' }, 400);
  }
  const wait = await spend(request, 3);
  if (wait) {
    return json({ error: `Too many requests from this address. Wait ${wait} seconds.` }, 429, {
      'retry-after': String(wait),
      'ratelimit-policy': policy(),
    });
  }
  const t = await table(env);
  if (!t) {
    return json(
      {
        error:
          'The catalogue is not loaded yet, so a link cannot be matched. Browse by government at https://publicdata.au/browse/',
      },
      503,
    );
  }
  const r = await resolve(env, t.tbl, url.href);
  if (!r) {
    return json({
      status: 'not_found',
      url: url.href,
      catalogue_read: t.version,
      message: `That page is not in the catalogue read on ${t.version}. It may be newer than that or on a portal we do not read. Send the link to National Digital, who run this site, and a person will look at it.`,
      contact: CONTACT,
    });
  }
  const record = shape(r);
  if (r.state === 'served') {
    return json({ status: 'served', record, page: record.page });
  }
  if (r.state === 'closed') {
    return json({ status: 'closed', record, reason: record.reason });
  }
  const v = await castVote({ request, env, params: { slug: r.vote } });
  const b = await v.json();
  if (!v.ok) {
    return json({ error: b.error || 'The vote was not counted' }, v.status);
  }
  return json({ status: 'voted', record, votes: b.votes });
}
