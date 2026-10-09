// download_name() in pipeline/publicdata/site.py: data.csv of qld-x 2026-04-24 saves as
// qld-x_2026-04-24.csv. pipeline/tests/fixtures/download_names.json holds the two together.
export function downloadName(slug, version, rel) {
  const tail = rel.startsWith('data.') ? rel.slice(4) : '_' + (rel.startsWith('parts/') ? rel.slice(6) : rel).replace(/\//g, '_');
  return slug + (version ? '_' + version : '') + tail;
}

const FILE = /^d\/([a-z0-9-]+)\/(?:v|fetch)\/(\d{4}-\d{2}-\d{2})\/(.+\.[a-z0-9]+)$/i;
const LOG = /^d\/([a-z0-9-]+)\/changes\/(?:(\d{4}-\d{2}-\d{2})|index)\.json$/;
const ARCHIVE = /^d\/([a-z0-9-]+)\/history\.tar\.zst$/;

// The Content-Disposition for a file that saves under a name of its own, or null for a page or a
// folder. A file under fetch/ is named for its fetch's date, the history archive for the newest
// version it holds, which latest.json names.
export function disposition(key, latest = {}) {
  const inline = (name) => `inline; filename="${name}"`;
  let m = key.match(FILE);
  if (m) return /^index\.(html|md)$/.test(m[3]) ? null : inline(downloadName(m[1], m[2], m[3]));
  if ((m = key.match(LOG))) return inline(m[2] ? downloadName(m[1], m[2], 'changes.json') : downloadName(m[1], '', 'changes/index.json'));
  if ((m = key.match(ARCHIVE))) return inline(downloadName(m[1], Object.hasOwn(latest, m[1]) ? latest[m[1]] : '', 'history.tar.zst'));
  return null;
}
