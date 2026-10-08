import { terms } from './_catalogue.js';

// The datasets served here, searched through the index the deploy loads into D1 as _served
// (pipeline/publicdata/d1.py). Without it, catalog.json is read instead.
export const SERVED = '_served';
export const LIMIT = 50;

const pick = (r) => ({
  slug: r.slug,
  title: r.title,
  publisher: r.publisher,
  licence: r.licence,
  page: r.page,
  latest: r.latest,
});

export async function searchServed(env, q, limit = LIMIT) {
  if (!env.DB) return null;
  let t;
  try {
    t = await env.DB.prepare(
      'SELECT tbl FROM _versions WHERE slug = ? ORDER BY version DESC LIMIT 1',
    )
      .bind(SERVED)
      .first();
  } catch {
    return null;
  }
  if (!t) return null;
  const m = terms(q);
  try {
    const res = m
      ? await env.DB.prepare(
          `SELECT c.* FROM "${t.tbl}_fts" f JOIN "${t.tbl}" c ON c.rowid = f.rowid WHERE "${t.tbl}_fts" MATCH ? ORDER BY bm25("${t.tbl}_fts", 8.0, 1.0, 3.0, 4.0, 1.0), c.slug LIMIT ?`,
        )
          .bind(m, limit)
          .all()
      : await env.DB.prepare(`SELECT * FROM "${t.tbl}" ORDER BY title LIMIT ?`).bind(limit).all();
    return (res.results || []).map(pick);
  } catch {
    return null;
  }
}

// The old way, and the fallback: every live dataset's DCAT record, matched on its whole text.
export function scanCatalog(catalog, q, limit = LIMIT) {
  const s = String(q || '').toLowerCase();
  return (catalog.dataset || [])
    .filter((d) => JSON.stringify(d).toLowerCase().indexOf(s) >= 0)
    .slice(0, limit)
    .map((d) => ({
      slug: d.identifier,
      title: d.title,
      publisher: d.publisher && d.publisher.name,
      licence: d.license,
      page: d.landingPage,
      latest: d.distribution && d.distribution[0] && d.distribution[0].accessURL,
    }));
}
