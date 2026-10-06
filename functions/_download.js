// download_name() in pipeline/publicdata/site.py: data.csv of qld-x 2026-04-24 saves as
// qld-x_2026-04-24.csv. functions/_download.test.mjs holds the two together.
export function downloadName(slug, version, rel) {
  return slug + '_' + version + (rel.startsWith('data.') ? rel.slice(4) : '_' + rel.replace(/\//g, '_'));
}

const FILE = /^d\/([a-z0-9-]+)\/v\/(\d{4}-\d{2}-\d{2})\/(.+\.[a-z0-9]+)$/i;

// The Content-Disposition for a dated file, or null for a page or anything outside a version.
export function disposition(key) {
  const m = key.match(FILE);
  if (!m || /^index\.(html|md)$/.test(m[3])) return null;
  return `inline; filename="${downloadName(m[1], m[2], m[3])}"`;
}
