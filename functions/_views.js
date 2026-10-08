// Saved explorer dashboards. A dashboard is stored under the hash of what it shows, so saving
// the same one twice gives the same link, and a saved link never changes.
export const MAX_BYTES = 32768;
export const MAX_PANELS = 12;
export const PER_DAY = 100;
export const ID = /^[a-f0-9]{16}$/;
const DATE = /^\d{4}-\d{2}-\d{2}$/;
// Kept in step with PANEL_KEYS in pipeline/publicdata/static/explorer.js.
const PANEL_KEYS = new Set([
  'plugin',
  'title',
  'plugin_config',
  'columns_config',
  'group_by',
  'split_by',
  'columns',
  'filter',
  'sort',
  'expressions',
  'windows',
  'aggregates',
  'group_by_depth',
  'filter_op',
  'group_rollup_mode',
  'split_rollup_mode',
]);
const TOP_KEYS = new Set(['panels', 'layout', 'masters', 'global_filters']);
const obj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

function depth(v, d = 0) {
  if (d > 16) return d;
  if (v === null || typeof v !== 'object') return d;
  return Math.max(d, ...Object.values(v).map((x) => depth(x, d + 1)));
}

// Returns an error message, or null when the body is a dashboard this site can show.
export function problem(body, versions) {
  if (!obj(body)) return 'Send JSON with slug, version and workspace';
  if (typeof body.slug !== 'string' || !versions) return 'No such dataset';
  if (
    typeof body.version !== 'string' ||
    !DATE.test(body.version) ||
    !versions.includes(body.version)
  )
    return 'No such version of this dataset';
  const ws = body.workspace;
  if (!obj(ws) || !obj(ws.panels)) return 'workspace needs a panels object';
  for (const k of Object.keys(ws))
    if (!TOP_KEYS.has(k)) return `workspace has an unknown key: ${k}`;
  const panels = Object.entries(ws.panels);
  if (panels.length < 1 || panels.length > MAX_PANELS)
    return `A dashboard has 1 to ${MAX_PANELS} panels`;
  for (const [id, p] of panels) {
    if (!/^[\w-]{1,64}$/.test(id) || !obj(p))
      return 'Each panel needs a short id and a settings object';
    for (const k of Object.keys(p))
      if (!PANEL_KEYS.has(k)) return `A panel has an unknown key: ${k}`;
  }
  if (depth(ws) > 16) return 'workspace is nested too deeply';
  return null;
}

export function canonical(body) {
  return JSON.stringify({ slug: body.slug, version: body.version, workspace: body.workspace });
}

export async function viewId(text) {
  const d = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(d)]
    .slice(0, 8)
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}
