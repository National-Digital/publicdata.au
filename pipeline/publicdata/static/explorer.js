// The explorer: DuckDB-WASM holds one version's Parquet in the browser and Perspective draws the
// dashboard over it. The dashboard lives in the URL fragment; a short link stores it through the API.
const X = JSON.parse(document.getElementById('ex-data').textContent);
const $ = (id) => document.getElementById(id);
const TABLE = 'memory.records';
const status = $('x-status');
const host = $('x-host');
let viewer = null;
let current = null;
let saving = null;
// An embed is read-only unless its snippet asked for edit=1.
const EDIT = !X.embed || new URLSearchParams(location.search).get('edit') === '1';

function say(text, state) {
  status.textContent = text;
  status.dataset.state = state || '';
}

function toast(m) {
  const t = $('toast');
  if (!t) return;
  t.textContent = m;
  t.classList.add('show');
  clearTimeout(toast.t);
  toast.t = setTimeout(() => t.classList.remove('show'), 1600);
}

function mb(n) {
  return n >= 1e6
    ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + ' MB'
    : Math.max(1, Math.round(n / 1e3)) + ' KB';
}

const b64 = {
  enc(bytes) {
    let s = '';
    for (let i = 0; i < bytes.length; i += 0x8000)
      s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  },
  dec(s) {
    const bin = atob(s.replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(bin, (c) => c.charCodeAt(0));
  },
};

// A saved dashboard is capped at this size by the API, and a link is held to the same.
const MAX_BYTES = 32768;
const MAX_PANELS = 12;

async function pipe(bytes, stream, limit = Infinity) {
  const reader = new Blob([bytes]).stream().pipeThrough(stream).getReader();
  const parts = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    got += value.length;
    if (got > limit) {
      reader.cancel();
      throw new Error('too large');
    }
    parts.push(value);
  }
  const out = new Uint8Array(got);
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
}

function depth(v, d = 0) {
  if (d > 16 || v === null || typeof v !== 'object') return d;
  return Math.max(d, ...Object.values(v).map((x) => depth(x, d + 1)));
}

async function pack(state) {
  return b64.enc(
    await pipe(
      new TextEncoder().encode(JSON.stringify(state)),
      new CompressionStream('deflate-raw'),
    ),
  );
}

async function unpack(s) {
  const state = JSON.parse(
    new TextDecoder().decode(
      await pipe(b64.dec(s), new DecompressionStream('deflate-raw'), MAX_BYTES),
    ),
  );
  const panels = state && state.w && state.w.panels;
  if (
    !panels ||
    typeof panels !== 'object' ||
    Object.keys(panels).length > MAX_PANELS ||
    depth(state) > 16
  )
    throw new Error('not a dashboard');
  return state;
}

// Kept in step with PANEL_KEYS in functions/_views.js.
const PANEL_KEYS = [
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
];

// Only view settings travel; the table and theme are this page's, whatever a link says.
function clean(ws) {
  const panels = {};
  for (const [id, p] of Object.entries((ws && ws.panels) || {})) {
    if (!p || typeof p !== 'object') continue;
    const q = {};
    for (const k of PANEL_KEYS) if (p[k] !== undefined && p[k] !== null) q[k] = p[k];
    panels[id] = relabel(q);
  }
  const out = { panels };
  if (ws && ws.layout) out.layout = ws.layout;
  if (ws && Array.isArray(ws.masters) && ws.masters.length) out.masters = ws.masters;
  if (ws && Array.isArray(ws.global_filters) && ws.global_filters.length)
    out.global_filters = ws.global_filters.map(term);
  return out;
}

// Columns are named by label. Links and saved dashboards from before that name the raw fields,
// so those names are translated; a label is never a field name, so this is safe to repeat.
const LABELS = X.labels || {};
const TEXT = new Set((X.text || []).map((n) => LABELS[n] || n));
const YESNO = new Set((X.yesno || []).map((n) => LABELS[n] || n));
const col = (c) => (typeof c === 'string' && Object.hasOwn(LABELS, c) ? LABELS[c] : c);
function term(f) {
  if (!Array.isArray(f)) return f;
  const c = col(f[0]);
  // Years are text and true or false is Yes or No now, so an older filter's value is carried over.
  const fix = (x) =>
    TEXT.has(c) && typeof x === 'number'
      ? String(x)
      : YESNO.has(c) && typeof x === 'boolean'
        ? x
          ? 'Yes'
          : 'No'
        : x;
  return [c, f[1], Array.isArray(f[2]) ? f[2].map(fix) : fix(f[2]), ...f.slice(3)];
}
function relabel(p) {
  const alias = {};
  const exprs = {};
  for (const [k, v] of Object.entries(p.expressions || {})) {
    if (typeof v !== 'string') {
      exprs[k] = v;
      continue;
    }
    // Older dashboards charted a year through CAST("year" AS VARCHAR); the column itself does that now.
    const m = v.match(/^CAST\("([^"]+)" AS VARCHAR\)$/);
    if (m && TEXT.has(col(m[1]))) {
      alias[k] = col(m[1]);
      continue;
    }
    exprs[k] = v.replace(/"((?:[^"]|"")+)"/g, (all, n) =>
      Object.hasOwn(LABELS, n) ? `"${LABELS[n].replace(/"/g, '""')}"` : all,
    );
  }
  const c = (x) => alias[x] ?? col(x);
  const q = { ...p };
  if (p.expressions) q.expressions = exprs;
  for (const k of ['columns', 'group_by', 'split_by']) if (Array.isArray(p[k])) q[k] = p[k].map(c);
  if (Array.isArray(p.sort))
    q.sort = p.sort.map((s) => (Array.isArray(s) ? [c(s[0]), ...s.slice(1)] : s));
  if (Array.isArray(p.filter))
    q.filter = p.filter.map((f) => term(Array.isArray(f) ? [c(f[0]), ...f.slice(1)] : f));
  for (const k of ['aggregates', 'columns_config']) {
    if (p[k] && typeof p[k] === 'object')
      q[k] = Object.fromEntries(
        Object.entries(p[k])
          .filter(([n]) => !alias[n] || k !== 'aggregates')
          .map(([n, v]) => [c(n), v]),
      );
  }
  return q;
}

function ids(layout, acc = []) {
  if (!layout) return acc;
  if (layout.type === 'tab-layout') acc.push(...(layout.tabs || []));
  else (layout.children || []).forEach((c) => ids(c, acc));
  return acc;
}

function theme() {
  const forced = document.documentElement.dataset.theme;
  const dark = forced ? forced === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  return dark ? 'Pro Dark' : 'Pro Light';
}

function prepare(ws, active) {
  const w = clean(ws);
  const t = theme();
  for (const p of Object.values(w.panels)) Object.assign(p, { table: TABLE, theme: t });
  // A phone has no room for splits, so the panels become tabs.
  if (host.clientWidth < 720) {
    const tabs = ids(w.layout).filter((id) => w.panels[id]);
    for (const id of Object.keys(w.panels)) if (!tabs.includes(id)) tabs.push(id);
    w.layout = { type: 'tab-layout', tabs, selected: 0 };
  }
  w.active = active || null;
  return w;
}

async function initial() {
  const params = new URLSearchParams(location.search);
  const view = params.get('view');
  if (view && /^[a-f0-9]{16}$/.test(view)) {
    try {
      const r = await fetch(`${X.views}/${view}`);
      if (r.ok) {
        const saved = await r.json();
        if (saved.slug === X.slug) return { v: saved.version, w: saved.workspace, view };
      }
      say('That saved dashboard was not found, so the first dashboard is shown instead.');
    } catch {
      say('The saved dashboard could not be read, so the first dashboard is shown instead.');
    }
  }
  const m = location.hash.match(/^#x=([A-Za-z0-9_-]+)$/);
  if (m) {
    try {
      const s = await unpack(m[1]);
      if (s && typeof s === 'object') return { v: s.v, w: s.w };
    } catch {
      say('The dashboard in this link could not be read, so the first dashboard is shown instead.');
    }
  }
  return { v: null, w: X.defaults };
}

async function download(url, size, label) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${label} answered ${r.status}`);
  const total = Number(r.headers.get('content-length')) || size || 0;
  if (!r.body || !total) return new Uint8Array(await r.arrayBuffer());
  const reader = r.body.getReader();
  const parts = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    parts.push(value);
    got += value.length;
    if (label === 'data')
      say(
        `Downloading ${mb(total)} of data: ${Math.min(99, Math.round((got / total) * 100))}%`,
        'busy',
      );
  }
  const out = new Uint8Array(got);
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
}

async function engine() {
  const V = X.vendor;
  const [duckdb, perspective] = await Promise.all([
    import(V + 'duckdb.js'),
    import(V + '@perspective-dev/client/dist/cdn/perspective.js').then((m) => m.default),
    import(V + '@perspective-dev/viewer/dist/cdn/perspective-viewer.js'),
  ]);
  await Promise.all([
    import(V + '@perspective-dev/viewer-datagrid/dist/cdn/perspective-viewer-datagrid.js'),
    import(V + '@perspective-dev/viewer-charts/dist/cdn/perspective-viewer-charts.js'),
  ]);
  const gz = await fetch(V + '@duckdb/duckdb-wasm/dist/duckdb-eh.wasm.gz');
  if (!gz.ok) throw new Error(`the query engine answered ${gz.status}`);
  const wasm = await new Response(gz.body.pipeThrough(new DecompressionStream('gzip'))).blob();
  const wasmURL = URL.createObjectURL(new Blob([wasm], { type: 'application/wasm' }));
  const worker = new Worker(
    new URL(V + '@duckdb/duckdb-wasm/dist/duckdb-browser-eh.worker.js', location.href),
  );
  const db = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(duckdb.LogLevel.WARNING), worker);
  await db.instantiate(wasmURL);
  URL.revokeObjectURL(wasmURL);
  const conn = await db.connect();
  // Perspective sorts nulls first ascending; DuckDB must agree or grouped rows jump about.
  await conn.query('SET default_null_order = NULLS_FIRST_ON_ASC_LAST_ON_DESC');
  await conn.query(
    `SET custom_extension_repository = '${new URL(V + 'duckdb-extensions', location.href).href}'`,
  );
  await conn.query('LOAD parquet');
  return { duckdb, perspective, db, conn };
}

const quote = (n) => '"' + String(n).replace(/"/g, '""') + '"';

const MONTHS = [
  'january',
  'february',
  'march',
  'april',
  'may',
  'june',
  'july',
  'august',
  'september',
  'october',
  'november',
  'december',
];
const DAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'];
const lead = (v) => v.trim().match(/^[<>≤≥]?\s*(\d+(?:\.\d+)?)/);

// Charts and menus sort text alphabetically, which scrambles months, weekdays and bands such as
// "5 to 9" and "10 to 14". A column whose values are one of those gets their natural order.
function natural(values) {
  const low = values.map((v) => v.trim().toLowerCase());
  for (const names of [MONTHS, DAYS]) {
    const at = (v) => names.findIndex((m) => v === m || (v.length >= 3 && m.startsWith(v)));
    if (low.every((v) => at(v) >= 0))
      return values
        .map((v, i) => [at(low[i]), v])
        .sort((a, b) => a[0] - b[0])
        .map((x) => x[1]);
  }
  const num = values.filter((v) => lead(v));
  if (num.length >= 3 && num.length * 3 >= values.length * 2) {
    const rest = values.filter((v) => !lead(v)).sort();
    return [
      ...num.sort((a, b) => parseFloat(lead(a)[1]) - parseFloat(lead(b)[1]) || a.localeCompare(b)),
      ...rest,
    ];
  }
  return null;
}

async function table(e, bytes) {
  await e.db.registerFileBuffer('data.parquet', bytes);
  const src = "read_parquet('data.parquet')";
  const described = (await e.conn.query(`DESCRIBE SELECT * FROM ${src}`)).toArray();
  const cols = described.map((r) => r.column_name);
  const text = new Set(X.text || []);
  const yesno = new Set(X.yesno || []);
  const int32 = new Set(X.int32);
  const strings = described.filter((r) => r.column_type === 'VARCHAR').map((r) => r.column_name);
  const order = {};
  if (strings.length) {
    const counts = (
      await e.conn.query(
        `SELECT ${strings.map((n, i) => `approx_count_distinct(${quote(n)}) AS c${i}`).join(', ')} FROM ${src}`,
      )
    ).toArray()[0];
    for (const [i, n] of strings.entries()) {
      if (Number(counts[`c${i}`]) > 150) continue;
      const values = (
        await e.conn.query(
          `SELECT DISTINCT ${quote(n)} AS v FROM ${src} WHERE ${quote(n)} IS NOT NULL LIMIT 200`,
        )
      )
        .toArray()
        .map((r) => r.v);
      const sorted = values.length <= 150 && natural(values);
      if (!sorted) continue;
      // An ENUM keeps the text and sorts in the order it lists.
      order[n] = `o${i}`;
      await e.conn.query(
        `CREATE OR REPLACE TYPE o${i} AS ENUM (${sorted.map((v) => `'${v.replace(/'/g, "''")}'`).join(', ')})`,
      );
    }
  }
  const select = cols.map((n) => {
    const q = quote(n);
    const v = text.has(n)
      ? `CAST(${q} AS VARCHAR)`
      : yesno.has(n)
        ? `CASE WHEN ${q} THEN 'Yes' WHEN NOT ${q} THEN 'No' END`
        : order[n]
          ? `CAST(${q} AS ${order[n]})`
          : int32.has(n)
            ? `CAST(${q} AS INTEGER)`
            : q;
    return `${v} AS ${quote(LABELS[n] || n)}`;
  });
  await e.conn.query(`CREATE OR REPLACE TABLE records AS SELECT ${select.join(', ')} FROM ${src}`);
  await e.db.dropFile('data.parquet');
}

async function snapshot() {
  const ws = clean(await viewer.saveWorkspace());
  const s = { w: ws };
  if (current.version !== X.versions[0].version) s.v = current.version;
  return s;
}

let timer = null;
function remember() {
  if (!viewer || !EDIT) return;
  clearTimeout(timer);
  timer = setTimeout(async () => {
    const packed = await pack(await snapshot());
    // An embed keeps its own address; its explorer link carries the reader's changes instead.
    if (X.embed) {
      $('x-open').href = `${location.origin}${X.page}#x=${packed}`;
      return;
    }
    history.replaceState(null, '', `${location.pathname}#x=${packed}`);
    $('x-short').hidden = true;
  }, 400);
}

// Each panel's title bar is its own shadow root, which the page's stylesheet cannot reach.
const TAB_CSS = new CSSStyleSheet();
TAB_CSS.replaceSync(
  `.psp-tab-title{font-size:13px;font-weight:600}
#rows{display:none !important}
.psp-tab-master::before{content:"Click to filter the others";font:600 11px "Random Grotesque","RG Fallback",Arial,sans-serif;color:#c24a11}
` +
    (EDIT
      ? `.psp-tab-settings{width:auto;min-width:0;padding:0 8px 0 6px;margin-left:6px;border:1px solid currentColor;border-radius:4px;opacity:.85;cursor:pointer}
.psp-tab-settings::after{content:"Edit";font:600 11px/18px "Random Grotesque","RG Fallback",Arial,sans-serif;letter-spacing:.02em}
.psp-tab-settings:hover{opacity:1;color:#f26324}
:host(:not(.visible)) .psp-tab-settings{border:0;padding:0;margin:0}
:host(:not(.visible)) .psp-tab-settings::after{content:none}`
      : '.psp-tab-settings,.psp-tab-caret{display:none}'),
);
function styleTabs() {
  for (const t of viewer.querySelectorAll('perspective-viewer-tab')) {
    const r = t.shadowRoot;
    if (r && !r.adoptedStyleSheets.includes(TAB_CSS))
      r.adoptedStyleSheets = [...r.adoptedStyleSheets, TAB_CSS];
  }
}

// The handler shares one DuckDB connection, and panels ask in parallel; answers that overlap
// come back mixed, so its queries run one at a time.
function serial(handler) {
  let chain = Promise.resolve();
  return new Proxy(handler, {
    get(target, key) {
      const v = Reflect.get(target, key);
      if (typeof v !== 'function') return v;
      if (key === 'getFeatures') return v.bind(target);
      return (...args) => {
        const run = chain.then(() => v.apply(target, args));
        chain = run.catch(() => {});
        return run;
      };
    },
  });
}

async function draw(e, ws, active) {
  const handler = await e.perspective.createMessageHandler(
    serial(new e.duckdb.DuckDBHandler(e.conn)),
  );
  const client = await e.perspective.worker(handler);
  viewer = document.createElement('perspective-viewer');
  host.replaceChildren(viewer);
  new MutationObserver(styleTabs).observe(viewer, { childList: true });
  await viewer.load(client);
  try {
    await viewer.restoreWorkspace(prepare(ws, active));
  } catch (err) {
    console.warn(err);
    say(
      'Part of that dashboard does not fit this version of the data, so the first dashboard is shown instead.',
    );
    await viewer.restoreWorkspace(prepare(X.defaults));
  }
  await viewer.flush();
  for (const ev of [
    'perspective-config-update',
    'perspective-layout-update',
    'perspective-global-filter-update',
  ]) {
    viewer.addEventListener(ev, remember);
  }
  // Dragging a divider changes the layout without an event.
  viewer.addEventListener('pointerup', remember);
}

async function save() {
  const s = await snapshot();
  const r = await fetch(X.views, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ slug: X.slug, version: current.version, workspace: s.w }),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `the API answered ${r.status}`);
  return body.id;
}

function busy(btn, on) {
  btn.disabled = on;
  btn.setAttribute('aria-busy', on ? 'true' : 'false');
}

function bindToolbar() {
  const ver = $('x-version');
  ver.value = current.version;
  ver.addEventListener('change', async () => {
    const s = await snapshot();
    s.v = ver.value;
    history.replaceState(null, '', `${X.page}#x=${await pack(s)}`);
    location.reload();
  });
  $('x-link').addEventListener('click', async () => {
    const s = await snapshot();
    const url = `${location.origin}${location.pathname}#x=${await pack(s)}`;
    try {
      await navigator.clipboard.writeText(url);
      toast('Link copied');
    } catch {
      toast('Copy the address bar');
    }
  });
  $('x-save').addEventListener('click', async (ev) => {
    const btn = ev.currentTarget;
    busy(btn, true);
    try {
      const id = await save();
      const url = `${location.origin}${X.page}?view=${id}`;
      history.replaceState(null, '', `${X.page}?view=${id}`);
      $('x-short-url').textContent = url;
      $('x-short').hidden = false;
      $('x-embed').hidden = true;
      toast('Saved');
    } catch (err) {
      toast('Not saved: ' + err.message);
    } finally {
      busy(btn, false);
    }
  });
  $('x-embed-btn').addEventListener('click', async (ev) => {
    const btn = ev.currentTarget;
    busy(btn, true);
    try {
      const id = await save();
      const src = `${location.origin}${X.embed_page}?view=${id}${$('x-embed-edit').checked ? '&edit=1' : ''}`;
      $('x-embed-code').textContent =
        `<iframe src="${src}" title="${X.title.replace(/"/g, '&quot;')}" width="100%" height="560" style="border:0" loading="lazy"></iframe>`;
      $('x-embed').hidden = false;
      $('x-short').hidden = true;
    } catch (err) {
      toast('Not saved: ' + err.message);
    } finally {
      busy(btn, false);
    }
  });
  $('x-reset').addEventListener('click', async () => {
    await viewer.restoreWorkspace(prepare(X.defaults));
    remember();
  });
  $('x-add').addEventListener('click', async (ev) => {
    const btn = ev.currentTarget;
    busy(btn, true);
    try {
      const n = (await viewer.getPanelNames()).length;
      if (n >= MAX_PANELS) {
        toast(`A dashboard holds ${MAX_PANELS} panels`);
        return;
      }
      const first = X.defaults.panels['by-group'] || {};
      const id = await viewer.addPanel({
        table: TABLE,
        theme: theme(),
        plugin: 'Y Bar',
        title: 'New chart',
        group_by: first.group_by || [],
        columns: first.columns || [],
        expressions: first.expressions || {},
        aggregates: first.aggregates || {},
      });
      await viewer.restoreWorkspace({ active: id });
      remember();
    } catch (err) {
      toast('No chart added: ' + err.message);
    } finally {
      busy(btn, false);
    }
  });
  $('x-embed-edit').addEventListener('change', () => {
    const code = $('x-embed-code');
    code.textContent = code.textContent.replace(
      /\?view=([a-f0-9]{16})(&edit=1)?/,
      (m, id) => `?view=${id}${$('x-embed-edit').checked ? '&edit=1' : ''}`,
    );
  });
  $('x-bar').hidden = false;
  $('x-hint').hidden = false;
}

// A visitor's first explorer, on any dataset, opens the first chart's settings so the editing shows
// before anyone has to look for it. Once seen it stays closed everywhere, so the key is site-wide.
function intro(start) {
  if (X.embed || start.w !== X.defaults || document.documentElement.clientWidth < 720) return null;
  try {
    if (localStorage.getItem('x-intro') === '1') return null;
    localStorage.setItem('x-intro', '1');
  } catch {
    return null;
  }
  return ids(X.defaults.layout)[0] || null;
}

async function main() {
  if (!('DecompressionStream' in window) || typeof WebAssembly !== 'object') {
    say(
      'This browser cannot run the explorer. The files and the query API on the dataset page work everywhere.',
      'error',
    );
    return;
  }
  const start = await initial();
  current = X.versions.find((v) => v.version === start.v) || X.versions[0];
  say('Starting the query engine', 'busy');
  const [e, bytes] = await Promise.all([engine(), download(current.parquet, current.size, 'data')]);
  say(`Preparing ${current.rows_fmt} rows`, 'busy');
  await table(e, bytes);
  say('Drawing', 'busy');
  await draw(e, start.w, intro(start));
  if ($('x-ghost')) $('x-ghost').hidden = true;
  if (!X.embed) bindToolbar();
  say(
    `${current.rows_fmt} rows, version ${current.version}. Everything runs in this browser.`,
    'ready',
  );
  if (X.embed)
    $('x-open').href =
      `${location.origin}${X.page}${start.view ? `?view=${start.view}` : location.hash}`;
  if (start.view && !X.embed) {
    $('x-short-url').textContent = `${location.origin}${X.page}?view=${start.view}`;
    $('x-short').hidden = false;
  }
}

main().catch((err) => {
  console.error(err);
  say(
    `The explorer stopped: ${err.message}. The files and the query API on the dataset page still work.`,
    'error',
  );
});
