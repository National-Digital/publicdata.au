(function () {
  'use strict';
  // The build replaces this exact text (API_SLOT in site.py).
  // prettier-ignore
  var SPEC = /*API_SPEC*/null;
  var toast = document.getElementById('toast'),
    tt;
  function say(m) {
    if (!toast) {
      return;
    }
    toast.textContent = m;
    toast.classList.add('show');
    clearTimeout(tt);
    tt = setTimeout(function () {
      toast.classList.remove('show');
    }, 1400);
  }

  document.querySelectorAll('[data-copy-target]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var el = document.getElementById(btn.dataset.copyTarget);
      var txt = (el.value !== undefined ? el.value : el.textContent).trim();
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(txt).then(
          function () {
            say('Copied');
          },
          function () {
            say('Select and copy');
          }
        );
      } else {
        say('Select and copy');
      }
    });
  });

  // Dataset page: format, version, URL, per-tool snippets.
  var getter = document.getElementById('getter');
  if (getter) {
    var D = JSON.parse(document.getElementById('ds-data').textContent);
    var seg = document.getElementById('fmt-seg'),
      ver = document.getElementById('ver');
    var curTool = 'curl';
    function fmt() {
      return seg.querySelector('[aria-pressed="true"]').dataset.fmt;
    }
    // A version's file on this site over http(s), so the page data cannot make the link a script.
    var FILE_URL =
      /^https?:\/\/[a-z0-9.-]+(?::\d+)?\/d\/[a-z0-9-]+\/(?:latest|v\/\d{4}-\d{2}-\d{2})\/[A-Za-z0-9_./-]+$/;
    function url() {
      var v = ver ? ver.value : 'latest',
        f = fmt();
      var file = D.formats[f].file;
      var u = D.base + (v === 'latest' ? 'latest/' : 'v/' + v + '/') + file;
      return FILE_URL.test(u) ? u : '';
    }
    // download_name() in site.py, with the suffix it gives each format and the date latest/ serves.
    // A page cached from before a deploy lacks both for five minutes, so the older rule stands in.
    function saveAs() {
      var v = ver ? ver.value : 'latest',
        d = D.formats[fmt()],
        file = d.file;
      var tail =
        d.suffix !== undefined && d.suffix !== null
          ? d.suffix
          : file.indexOf('data.') === 0
            ? file.slice(4)
            : '_' + file.replace(/\//g, '_');
      return D.slug + '_' + (v === 'latest' ? D.served || D.latest : v) + tail;
    }
    function quote(s) {
      return '"' + s + '"';
    }
    function tools(f, u) {
      var t = {},
        name = u.split('/').pop(),
        tbl = D.slug.replace(/-/g, '_'),
        key = D.example_field;
      t['curl'] = 'curl -L -o ' + name + ' ' + u;
      if (f === 'json' || f === 'partition') {
        t['Python'] =
          'import requests\nr = requests.get(' +
          quote(u) +
          ').json()\nrows = r["records"]\nprint(r["publicdata"]["attribution"])';
      }
      if (f === 'csv') {
        t['Python'] = 'import pandas as pd\ndf = pd.read_csv(' + quote(u) + ')\ndf.head()';
      }
      if (f === 'parquet') {
        t['Python'] =
          'import pandas as pd\ndf = pd.read_parquet(' +
          quote(u) +
          ')\ndf.groupby(' +
          quote(key) +
          ').size()';
      }
      if (f === 'ndjson') {
        t['Python'] =
          'import pandas as pd\ndf = pd.read_json(' + quote(u) + ', lines=True, skiprows=1)';
      }
      if (f === 'sqlite') {
        t['Python'] =
          'import sqlite3, urllib.request\nurllib.request.urlretrieve(' +
          quote(u) +
          ', ' +
          quote(tbl + '.sqlite') +
          ')\ncon = sqlite3.connect(' +
          quote(tbl + '.sqlite') +
          ')\ncon.execute("select ' +
          key +
          ', count(*) from records group by 1").fetchall()';
      }
      if (f === 'duckdb') {
        t['Python'] =
          'import duckdb\ncon = duckdb.connect()\ncon.execute("INSTALL httpfs; LOAD httpfs")\ncon.execute("ATTACH \'' +
          u +
          "' AS " +
          tbl +
          ' (READ_ONLY)")\ncon.sql("select ' +
          key +
          ', count(*) from ' +
          tbl +
          '.records group by 1 order by 2 desc").df()';
      }
      if (f === 'geojson') {
        t['Python'] = 'import geopandas as gpd\ngdf = gpd.read_file(' + quote(u) + ')\ngdf.plot()';
      }
      if (f === 'gpkg') {
        t['Python'] =
          'import geopandas as gpd\ngdf = gpd.read_file(' +
          quote(u) +
          ', layer="records")\ngdf.plot()';
      }
      if (f === 'xlsx') {
        t['Python'] =
          'import pandas as pd\ndf = pd.read_excel(' +
          quote(u) +
          ', sheet_name="records")\ndf.head()';
      }
      if (f === 'arrow') {
        t['Python'] =
          'import polars as pl\ndf = pl.read_ipc(' +
          quote(u) +
          ')\n# or: import pyarrow.feather as pf; t = pf.read_table("' +
          name +
          '")';
      }
      if (f === 'csv.gz') {
        t['Python'] =
          'import pandas as pd\ndf = pd.read_csv(' + quote(u) + ', compression="gzip")\ndf.head()';
      }
      if (f === 'parquet') {
        t['DuckDB'] =
          'select ' +
          key +
          ", count(*)\nfrom read_parquet('" +
          u +
          "')\ngroup by 1 order by 2 desc;";
      }
      if (f === 'csv') {
        t['DuckDB'] = "select * from read_csv_auto('" + u + "') limit 10;";
      }
      if (f === 'json' || f === 'partition') {
        t['DuckDB'] =
          "select r.* from (select unnest(records) as r from read_json_auto('" +
          u +
          "')) limit 10;";
      }
      if (f === 'ndjson') {
        t['DuckDB'] = "select * from read_ndjson_auto('" + u + "', ignore_errors=true) limit 10;";
      }
      if (f === 'sqlite') {
        t['DuckDB'] =
          "install sqlite; load sqlite;\nattach '" +
          name +
          "' as src (type sqlite);\nselect count(*) from src.records;";
      }
      if (f === 'duckdb') {
        t['DuckDB'] =
          "install httpfs; load httpfs;\nattach '" +
          u +
          "' as " +
          tbl +
          ' (read_only);\nselect ' +
          key +
          ', count(*) from ' +
          tbl +
          '.records group by 1 order by 2 desc;';
      }
      if (f === 'geojson' || f === 'gpkg') {
        t['DuckDB'] =
          "install spatial; load spatial;\nselect * from st_read('" +
          u +
          "'" +
          (f === 'gpkg' ? ", layer='records'" : '') +
          ') limit 10;';
      }
      if (f === 'csv.gz') {
        t['DuckDB'] = "select * from read_csv_auto('" + u + "') limit 10;";
      }
      if (f === 'arrow') {
        t['DuckDB'] = "-- download first, then\nselect * from '" + name + "' limit 10;";
      }
      if (f === 'xlsx') {
        t['DuckDB'] =
          "install excel; load excel;\nselect * from read_xlsx('" +
          name +
          "', sheet='records') limit 10;";
      }
      if (f === 'arrow') {
        t['JavaScript'] =
          'import { tableFromIPC } from "apache-arrow";\nconst table = tableFromIPC(await fetch(' +
          quote(u) +
          ').then(r => r.arrayBuffer()));\nconsole.log(table.numRows, table.schema.metadata.get("publicdata"));';
      }
      if (f === 'json' || f === 'partition' || f === 'ndjson' || f === 'csv' || f === 'geojson') {
        t['JavaScript'] =
          'const res = await fetch(' +
          quote(u) +
          ');\n' +
          (f === 'csv'
            ? 'const text = await res.text(); // parse with d3-dsv or PapaParse'
            : f === 'ndjson'
              ? 'const lines = (await res.text()).trim().split("\\n").map(JSON.parse);\nconst header = lines.shift().publicdata;'
              : f === 'geojson'
                ? 'const geo = await res.json(); // a FeatureCollection, ready for Leaflet or MapLibre'
                : 'const data = await res.json();\nconsole.log(data.publicdata.attribution, data.records.length);');
      }
      if (f === 'csv') {
        t['R'] = 'df <- read.csv(' + quote(u) + ')';
      }
      if (f === 'parquet') {
        t['R'] = 'library(arrow)\ndf <- read_parquet(' + quote(u) + ')';
      }
      if (f === 'json' || f === 'partition') {
        t['R'] = 'library(jsonlite)\nx <- fromJSON(' + quote(u) + ')\ndf <- x$records';
      }
      if (f === 'geojson') {
        t['R'] = 'library(sf)\nshapes <- st_read(' + quote(u) + ')';
      }
      if (f === 'gpkg') {
        t['R'] = 'library(sf)\nshapes <- st_read(' + quote(u) + ', layer = "records")';
      }
      if (f === 'arrow') {
        t['R'] = 'library(arrow)\ndf <- read_feather(' + quote(u) + ')';
      }
      if (f === 'xlsx') {
        t['R'] =
          'library(readxl)\ndownload.file(' +
          quote(u) +
          ', ' +
          quote(name) +
          ')\ndf <- read_excel(' +
          quote(name) +
          ', sheet = "records")';
      }
      if (f === 'csv.gz') {
        t['R'] = 'df <- read.csv(gzcon(url(' + quote(u) + ')))';
      }
      if (f === 'duckdb') {
        t['R'] =
          'library(DBI)\ncon <- dbConnect(duckdb::duckdb())\ndbExecute(con, "INSTALL httpfs; LOAD httpfs")\ndbExecute(con, "ATTACH \'' +
          u +
          "' AS " +
          tbl +
          ' (READ_ONLY)")\ndbGetQuery(con, "select ' +
          key +
          ', count(*) from ' +
          tbl +
          '.records group by 1 order by 2 desc")\n# or: publicdataau::pd_connect("' +
          D.slug +
          '")';
      }
      if (f === 'csv') {
        t['Excel, Sheets, Power BI'] =
          'Excel and Power BI: Data > Get Data > From Web, paste the URL.\nGoogle Sheets: =IMPORTDATA(' +
          quote(u) +
          ')';
      }
      if (f === 'xlsx') {
        t['Excel, Sheets, Power BI'] =
          'Excel: download and open. The rows are on the "records" sheet.\nPower BI: Data > Get Data > Excel workbook.\nGoogle Sheets: File > Import > Upload.';
      }
      if (f === 'json' || f === 'partition') {
        t['Excel, Sheets, Power BI'] =
          'Power BI and Excel: Data > Get Data > From Web, paste the URL, then expand "records" in Power Query.';
      }
      if (f === 'geojson') {
        t['QGIS'] = 'Layer > Add Layer > Add Vector Layer > Protocol: HTTP(S), paste the URL.';
      }
      if (f === 'gpkg') {
        t['QGIS'] =
          'Download the file and drag it onto the map. The layer is "records".\nArcGIS Pro: Add Data > browse to the .gpkg > records.';
      }
      return t;
    }
    function render() {
      var f = fmt(),
        u = url();
      document.getElementById('url').textContent = u;
      document.getElementById('dl').setAttribute('href', u);
      document.getElementById('dl').setAttribute('download', saveAs());
      getter.querySelectorAll('[data-note]').forEach(function (n) {
        n.hidden = n.getAttribute('data-note') !== f;
      });
      var t = tools(f, u),
        names = Object.keys(t);
      if (names.indexOf(curTool) < 0) {
        curTool = names[0];
      }
      var tb = document.getElementById('tools');
      tb.innerHTML = '';
      names.forEach(function (n) {
        var b = document.createElement('button');
        b.setAttribute('role', 'tab');
        b.type = 'button';
        b.textContent = n;
        b.setAttribute('aria-selected', n === curTool ? 'true' : 'false');
        b.addEventListener('click', function () {
          curTool = n;
          render();
        });
        tb.appendChild(b);
      });
      document.getElementById('snippet-code').textContent = t[curTool];
    }
    seg.querySelectorAll('button').forEach(function (b) {
      b.addEventListener('click', function () {
        seg.querySelectorAll('button').forEach(function (x) {
          x.setAttribute('aria-pressed', 'false');
        });
        b.setAttribute('aria-pressed', 'true');
        render();
      });
    });
    if (ver) {
      ver.addEventListener('change', render);
    }
    render();
  }

  // Dataset page: a query builder over the query API. Nothing is fetched until Run.
  var qc = document.getElementById('console');
  var QC = qc && JSON.parse(document.getElementById('ds-data').textContent).console;
  // The console's links stay on this site's query API.
  if (QC && /^\/api\/v1\/datasets\/[a-z0-9-]+\/$/.test(QC.api)) {
    var qApi = QC.api;
    var F = {};
    QC.fields.forEach(function (f) {
      F[f.name] = f;
    });
    var NUM = { integer: 1, number: 1, date: 1, datetime: 1 };
    var OPL = {};
    Object.keys(SPEC.operators).forEach(function (k) {
      OPL[k] = SPEC.operators[k].label;
    });
    OPL.nul = SPEC.operators['is.null'].label;
    OPL.notnul = SPEC.operators['is.null'].not_label;
    var opsFor = function (t) {
      if (t === 'boolean') {
        return ['eq', 'nul', 'notnul'];
      }
      if (NUM[t]) {
        return ['eq', 'neq', 'gte', 'lte', 'gt', 'lt', 'in', 'nul', 'notnul'];
      }
      return ['eq', 'neq', 'ilike', 'in', 'nul', 'notnul'];
    };
    var el = function (tag, props, kids) {
      var n = document.createElement(tag);
      for (var k in props || {}) {
        if (k === 'text') {
          n.textContent = props[k];
        } else if (k === 'class') {
          n.className = props[k];
        } else {
          n.setAttribute(k, props[k]);
        }
      }
      (kids || []).forEach(function (c) {
        if (c) {
          n.appendChild(c);
        }
      });
      return n;
    };
    var options = function (sel, list, cur) {
      sel.innerHTML = '';
      list.forEach(function (o) {
        var x = el('option', { value: o[0], text: o[1] });
        if (o[0] === cur) {
          x.selected = true;
        }
        sel.appendChild(x);
      });
    };
    var S = {
      mode: 'rows',
      version: '',
      format: 'json',
      rowsLimit: 20,
      aggLimit: 100,
      filters: QC.example.filters.map(function (f) {
        return { field: f.field, op: f.op, value: f.value };
      }),
      select: [],
      order: '',
      dir: 'asc',
      group: QC.example.group.slice(),
      metric: QC.example.metric,
      aggOrder: '',
    };
    var numeric = QC.fields.filter(function (f) {
      return f.type === 'integer' || f.type === 'number' || f.type === 'boolean';
    });
    var groupable = QC.fields.filter(function (f) {
      return f.values || QC.example.group.indexOf(f.name) >= 0;
    });

    QC.fields.forEach(function (f) {
      if (!f.values) {
        return;
      }
      var dl = el('datalist', { id: 'qv-' + f.name });
      f.values.forEach(function (v) {
        dl.appendChild(el('option', { value: String(v) }));
      });
      qc.appendChild(dl);
    });

    var row = function (label, body) {
      return el('div', { class: 'row' }, [el('span', { class: 'lab', text: label }), body]);
    };
    var modeSeg = el('div', { class: 'seg', role: 'group', 'aria-label': 'Ask for' });
    [
      ['rows', 'Rows'],
      ['aggregate', 'Counts and sums'],
    ].forEach(function (m) {
      var b = el('button', {
        type: 'button',
        'data-mode': m[0],
        'aria-pressed': m[0] === S.mode ? 'true' : 'false',
        text: m[1],
      });
      b.addEventListener('click', function () {
        S.mode = m[0];
        paint();
      });
      modeSeg.appendChild(b);
    });
    var frows = el('div', { class: 'frows' });
    var addF = el('button', { type: 'button', class: 'copy', text: 'Add a filter' });
    addF.addEventListener('click', function () {
      S.filters.push({ field: QC.fields[0].name, op: 'eq', value: '' });
      paintFilters();
      update();
      var i = frows.querySelectorAll('select');
      if (i.length) {
        i[i.length - 2].focus();
      }
    });

    var colsBox = el('details', { class: 'cols-box' });
    var colsSum = el('summary', {});
    colsBox.appendChild(colsSum);
    var cols = el('div', { class: 'cols' });
    colsBox.appendChild(cols);
    var clearB = el('button', { type: 'button', class: 'copy', text: 'Clear every tick' }),
      allB = el('button', { type: 'button', class: 'copy', text: 'Tick every field' });
    var tickAll = function (on) {
      cols.querySelectorAll('input').forEach(function (x) {
        x.checked = on;
      });
      cols.dispatchEvent(new Event('change'));
    };
    clearB.addEventListener('click', function () {
      tickAll(false);
    });
    allB.addEventListener('click', function () {
      tickAll(true);
    });
    colsBox.appendChild(el('div', { class: 'frow', style: 'margin-top:8px' }, [allB, clearB]));
    QC.fields.forEach(function (f) {
      var c = el('input', { type: 'checkbox', value: f.name });
      c.checked = true;
      cols.appendChild(el('label', {}, [c, el('span', { class: 'mono', text: f.name })]));
    });
    var orderSel = el('select', { 'aria-label': 'Order by' }),
      dirSel = el('select', { 'aria-label': 'Direction' });
    options(
      orderSel,
      [['', "Publisher's order"]].concat(
        QC.fields.map(function (f) {
          return [f.name, f.name];
        })
      ),
      ''
    );
    options(
      dirSel,
      [
        ['asc', 'Ascending'],
        ['desc', 'Descending'],
      ],
      'asc'
    );
    orderSel.addEventListener('change', function () {
      S.order = orderSel.value;
      dirSel.hidden = !S.order;
      update();
    });
    dirSel.addEventListener('change', function () {
      S.dir = dirSel.value;
      update();
    });
    dirSel.hidden = true;
    var limitIn = el('input', {
      type: 'number',
      min: '1',
      max: '10000',
      value: String(S.rowsLimit),
      'aria-label': 'Rows per page',
      class: 'num',
    });
    limitIn.addEventListener('input', function () {
      var n = parseInt(limitIn.value, 10);
      if (n >= 1 && n <= 10000) {
        if (S.mode === 'rows') {
          S.rowsLimit = n;
        } else {
          S.aggLimit = n;
        }
        update();
      }
    });

    var g1 = el('select', { 'aria-label': 'Group by' }),
      g2 = el('select', { 'aria-label': 'Then by' });
    var gOpts = groupable.map(function (f) {
      return [f.name, f.name];
    });
    options(g1, [['', 'Nothing (one total)']].concat(gOpts), S.group[0] || '');
    options(
      g2,
      [['', 'and nothing else']].concat(
        gOpts.map(function (o) {
          return [o[0], 'then ' + o[1]];
        })
      ),
      S.group[1] || ''
    );
    var regroup = function () {
      S.group = [g1.value, g2.value].filter(function (v, i, a) {
        return v && a.indexOf(v) === i;
      });
      g2.hidden = !g1.value;
      update();
    };
    g1.addEventListener('change', regroup);
    g2.addEventListener('change', regroup);
    g2.hidden = !g1.value;
    var fnSel = el('select', { 'aria-label': 'Measure' }),
      mfSel = el('select', { 'aria-label': 'Of field' });
    var m0 = S.metric.split('.');
    options(
      fnSel,
      [
        ['count', 'Count rows'],
        ['sum', 'Sum of'],
        ['avg', 'Average of'],
        ['min', 'Minimum of'],
        ['max', 'Maximum of'],
      ],
      m0[0]
    );
    options(
      mfSel,
      numeric.map(function (f) {
        return [f.name, f.name];
      }),
      m0[1] || (numeric[0] && numeric[0].name)
    );
    if (!numeric.length) {
      fnSel.disabled = true;
    }
    var remetric = function () {
      mfSel.hidden = fnSel.value === 'count';
      S.metric = fnSel.value === 'count' ? 'count' : fnSel.value + '.' + mfSel.value;
      update();
    };
    fnSel.addEventListener('change', remetric);
    mfSel.addEventListener('change', remetric);
    mfSel.hidden = fnSel.value === 'count';
    var aggOrd = el('select', { 'aria-label': 'Order groups' });
    options(
      aggOrd,
      [
        ['', 'In group order'],
        ['desc', 'Largest first'],
        ['asc', 'Smallest first'],
      ],
      ''
    );
    aggOrd.addEventListener('change', function () {
      S.aggOrder = aggOrd.value;
      update();
    });

    var verSel = el('select', { 'aria-label': 'Version' }),
      fmtSel = el('select', { 'aria-label': 'Format' });
    options(
      verSel,
      [['', 'Newest loaded']].concat(
        QC.versions.map(function (v) {
          return [v, v + ' (pinned)'];
        })
      ),
      ''
    );
    options(
      fmtSel,
      [
        ['json', 'JSON'],
        ['csv', 'CSV'],
        ['ndjson', 'NDJSON'],
      ],
      'json'
    );
    verSel.addEventListener('change', function () {
      S.version = verSel.value;
      update();
    });
    fmtSel.addEventListener('change', function () {
      S.format = fmtSel.value;
      update();
    });

    var urlBox = el('div', {
      class: 'path',
      id: 'q-url',
      tabindex: '0',
      role: 'region',
      'aria-label': 'Query URL',
    });
    var runB = el('button', { type: 'button', class: 'btn', text: 'Run' });
    var openA = el('a', { class: 'btn ghost', text: 'Open the result', rel: 'nofollow' });
    var copyB = el('button', { type: 'button', class: 'copy', text: 'Copy URL' });
    copyB.addEventListener('click', function () {
      var t = urlBox.textContent;
      if (navigator.clipboard) {
        navigator.clipboard.writeText(t).then(
          function () {
            say('Copied');
          },
          function () {
            say('Select and copy');
          }
        );
      } else {
        say('Select and copy');
      }
    });
    var tabs = el('div', { class: 'tools', role: 'tablist', 'aria-label': 'Query code' });
    var code = el('code', { id: 'q-code' });
    var copyC = el('button', { type: 'button', class: 'copy abs', text: 'Copy' });
    copyC.addEventListener('click', function () {
      if (navigator.clipboard) {
        navigator.clipboard.writeText(code.textContent).then(
          function () {
            say('Copied');
          },
          function () {
            say('Select and copy');
          }
        );
      } else {
        say('Select and copy');
      }
    });
    var status = el('p', { class: 'fmt-note', role: 'status', 'aria-live': 'polite' });
    var result = el('div', { class: 'tbl qres', hidden: '' });

    var rowsOnly = [
      row('Fields', colsBox),
      row('Order', el('div', { class: 'frow' }, [orderSel, dirSel])),
    ];
    var aggOnly = [
      row('Group by', el('div', { class: 'frow' }, [g1, g2])),
      row('Measure', el('div', { class: 'frow' }, [fnSel, mfSel])),
      row('Order', aggOrd),
    ];
    var st = document.getElementById('q-static');
    if (st) {
      st.remove();
    }
    [row('Ask for', modeSeg), row('Filters', el('div', { class: 'frows-wrap' }, [frows, addF]))]
      .concat(rowsOnly, aggOnly, [
        row('Limit', limitIn),
        row('Version', verSel),
        row('Format', fmtSel),
        el('div', { class: 'urlbox' }, [urlBox, copyB, runB, openA]),
        status,
        result,
        el('div', {}, [tabs, el('pre', {}, [copyC, code])]),
      ])
      .forEach(function (n) {
        qc.appendChild(n);
      });

    var fieldOpts = QC.fields.map(function (f) {
      return [f.name, f.name];
    });
    var paintFilters = function () {
      frows.innerHTML = '';
      if (!S.filters.length) {
        frows.appendChild(el('span', { class: 'fmt-note', text: 'None. Every row counts.' }));
      }
      S.filters.forEach(function (f, i) {
        var fs = el('select', { 'aria-label': 'Filter ' + (i + 1) + ' field' }),
          os = el('select', { 'aria-label': 'Filter ' + (i + 1) + ' test' });
        options(fs, fieldOpts, f.field);
        var t = F[f.field].type,
          ops = opsFor(t);
        if (ops.indexOf(f.op) < 0) {
          f.op = ops[0];
        }
        options(
          os,
          ops.map(function (o) {
            return [o, OPL[o]];
          }),
          f.op
        );
        var v;
        if (t === 'boolean') {
          v = el('select', { 'aria-label': 'Filter ' + (i + 1) + ' value' });
          options(
            v,
            [
              ['true', 'true'],
              ['false', 'false'],
            ],
            f.value === 'false' ? 'false' : 'true'
          );
          f.value = v.value;
        } else {
          var fd = F[f.field];
          v = el('input', {
            type: 'text',
            value: f.value,
            'aria-label': 'Filter ' + (i + 1) + ' value',
            spellcheck: 'false',
            autocomplete: 'off',
            placeholder:
              f.op === 'in'
                ? 'a,b,c'
                : 'min' in fd
                  ? fd.min + ' to ' + fd.max
                  : fd.values
                    ? 'for example ' + fd.values[0]
                    : '',
          });
          if (fd.values && f.op !== 'in') {
            v.setAttribute('list', 'qv-' + f.field);
          }
        }
        v.hidden = f.op === 'nul' || f.op === 'notnul';
        var rm = el('button', {
          type: 'button',
          class: 'copy',
          'aria-label': 'Remove filter ' + (i + 1),
          text: 'Remove',
        });
        fs.addEventListener('change', function () {
          f.field = fs.value;
          f.value = '';
          paintFilters();
          update();
        });
        os.addEventListener('change', function () {
          f.op = os.value;
          paintFilters();
          update();
        });
        v.addEventListener(v.tagName === 'SELECT' ? 'change' : 'input', function () {
          f.value = v.value;
          update();
        });
        rm.addEventListener('click', function () {
          S.filters.splice(i, 1);
          paintFilters();
          update();
          addF.focus();
        });
        frows.appendChild(el('div', { class: 'frow' }, [fs, os, v, rm]));
      });
    };

    var enc = function (s) {
      return encodeURIComponent(s).replace(/%2C/g, ',').replace(/%3A/g, ':');
    };
    var params = function (format) {
      var p = [];
      if (S.mode === 'aggregate') {
        if (S.group.length) {
          p.push(['group', S.group.join(',')]);
        }
        p.push(['metric', S.metric]);
      } else if (S.select.length) {
        p.push(['select', S.select.join(',')]);
      }
      S.filters.forEach(function (f) {
        var v;
        if (f.op === 'nul') {
          v = 'is.null';
        } else if (f.op === 'notnul') {
          v = 'not.is.null';
        } else if (f.value === '') {
          return;
        } else if (f.op === 'ilike') {
          v = 'ilike.*' + f.value + '*';
        } else if (f.op === 'in') {
          v = 'in.(' + f.value + ')';
        } else {
          v = f.op + '.' + f.value;
        }
        p.push([f.field, v]);
      });
      if (S.mode === 'rows' && S.order) {
        p.push(['order', S.order + '.' + S.dir]);
      }
      if (S.mode === 'aggregate' && S.aggOrder) {
        p.push(['order', S.metric.replace('.', '_') + '.' + S.aggOrder]);
      }
      p.push(['limit', String(S.mode === 'rows' ? S.rowsLimit : S.aggLimit)]);
      if (format && format !== 'json') {
        p.push(['format', format]);
      }
      return p;
    };
    var path = function (p) {
      return (
        qApi +
        (S.version ? 'versions/' + S.version + '/' : '') +
        S.mode +
        (p.length
          ? '?' +
            p
              .map(function (kv) {
                return kv[0] + '=' + enc(kv[1]);
              })
              .join('&')
          : '')
      );
    };
    var curTab = 'curl';
    var snippets = function () {
      var u = QC.site + path(params(S.format)),
        csv = QC.site + path(params('csv')),
        s = {},
        qq = function (x) {
          return '"' + x + '"';
        };
      s['curl'] = 'curl ' + qq(u);
      s['Python'] =
        S.format === 'json'
          ? 'import requests\nr = requests.get(' +
            qq(u) +
            ').json()\nrows = r["rows"]  # r["next"] is the next page, or None\nprint(r["publicdata"]["attribution"])'
          : 'import pandas as pd\ndf = pd.read_' +
            (S.format === 'csv' ? 'csv(' + qq(u) + ')' : 'json(' + qq(u) + ', lines=True)');
      s['pandas'] = 'import pandas as pd\ndf = pd.read_csv(' + qq(csv) + ')';
      s['JavaScript'] =
        S.format === 'json'
          ? 'const r = await fetch(' +
            qq(u) +
            ').then(res => res.json());\nconsole.log(r.rows, r.next, r.publicdata.attribution);'
          : 'const text = await fetch(' + qq(u) + ').then(res => res.text());';
      s['R'] =
        'library(jsonlite)\nx <- fromJSON(' +
        qq(QC.site + path(params('json'))) +
        ')\ndf <- x$rows';
      s['Sheets'] = '=IMPORTDATA(' + qq(csv) + ')';
      return s;
    };
    var update = function () {
      var u = path(params(S.format));
      urlBox.textContent = QC.site + u;
      openA.setAttribute('href', u);
      var s = snippets(),
        names = Object.keys(s);
      tabs.innerHTML = '';
      names.forEach(function (n) {
        var b = el('button', {
          type: 'button',
          role: 'tab',
          'aria-selected': n === curTab ? 'true' : 'false',
          text: n,
        });
        b.addEventListener('click', function () {
          curTab = n;
          update();
        });
        tabs.appendChild(b);
      });
      code.textContent = s[curTab];
    };
    var paint = function () {
      modeSeg.querySelectorAll('button').forEach(function (b) {
        b.setAttribute('aria-pressed', b.dataset.mode === S.mode ? 'true' : 'false');
      });
      rowsOnly.forEach(function (n) {
        n.hidden = S.mode !== 'rows';
      });
      aggOnly.forEach(function (n) {
        n.hidden = S.mode !== 'aggregate';
      });
      limitIn.value = String(S.mode === 'rows' ? S.rowsLimit : S.aggLimit);
      colsSum.textContent = colsLabel();
      update();
    };
    var colsLabel = function () {
      var n = cols.querySelectorAll('input:checked').length;
      return n === QC.fields.length
        ? 'All ' + n + ' fields'
        : n
          ? n + ' of ' + QC.fields.length + ' fields'
          : 'No field ticked, so every field is returned';
    };
    cols.addEventListener('change', function () {
      S.select = [].filter
        .call(cols.querySelectorAll('input'), function (x) {
          return x.checked;
        })
        .map(function (x) {
          return x.value;
        });
      if (S.select.length === QC.fields.length) {
        S.select = [];
      }
      colsSum.textContent = colsLabel();
      update();
    });

    var SHOW = 200,
      busy = false;
    var table = function (rows) {
      result.innerHTML = '';
      if (!rows.length) {
        result.hidden = true;
        return;
      }
      var keys = Object.keys(rows[0]);
      var thead = el('thead', {}, [
        el(
          'tr',
          {},
          keys.map(function (k) {
            return el('th', { text: k });
          })
        ),
      ]);
      var tbody = el('tbody');
      rows.slice(0, SHOW).forEach(function (r) {
        tbody.appendChild(
          el(
            'tr',
            {},
            keys.map(function (k) {
              var v = r[k];
              return v === null || v === undefined
                ? el('td', { class: 'nul', text: 'null' })
                : el('td', { class: typeof v === 'number' ? 'n' : '', text: String(v) });
            })
          )
        );
      });
      result.appendChild(el('table', { class: 'ledger' }, [thead, tbody]));
      result.hidden = false;
    };
    var run = function (offset) {
      if (busy) {
        return;
      }
      busy = true;
      runB.disabled = true;
      var p = params('json');
      if (offset) {
        p.push(['offset', String(offset)]);
      }
      var t0 = Date.now();
      status.textContent = 'Running the query.';
      fetch(path(p))
        .then(function (r) {
          return r
            .json()
            .catch(function () {
              return {};
            })
            .then(function (b) {
              return { r: r, b: b };
            });
        })
        .then(function (x) {
          var r = x.r,
            b = x.b,
            ms = Date.now() - t0;
          status.innerHTML = '';
          if (r.status === 429) {
            var wait = r.headers.get('Retry-After') || b.block_seconds || 10;
            status.textContent =
              'Too many requests from this address. Run again in ' + wait + ' seconds.';
            return;
          }
          if (!r.ok) {
            table([]);
            status.textContent =
              (r.status === 400 ? 'The API could not run that query: ' : '') +
              (b.error || 'The query API answered ' + r.status + '.');
            return;
          }
          var rows = b.rows || [];
          table(rows);
          var v = r.headers.get('X-Publicdata-Version') || (b.publicdata && b.publicdata.version);
          var msg = rows.length
            ? rows.length.toLocaleString('en-AU') +
              (S.mode === 'rows'
                ? rows.length === 1
                  ? ' row'
                  : ' rows'
                : rows.length === 1
                  ? ' group'
                  : ' groups') +
              (offset ? ' from row ' + (offset + 1).toLocaleString('en-AU') : '') +
              ' of version ' +
              v +
              ', in ' +
              ms +
              ' ms.' +
              (rows.length > SHOW
                ? ' The table shows the first ' + SHOW + '; the URL returns all of them.'
                : '')
            : 'No rows match. Loosen a filter and run again.';
          status.appendChild(document.createTextNode(msg + ' '));
          if (b.next) {
            var nb = el('button', { type: 'button', class: 'copy', text: 'Next page' });
            var lim = S.mode === 'rows' ? S.rowsLimit : S.aggLimit;
            nb.addEventListener('click', function () {
              run((offset || 0) + lim);
            });
            status.appendChild(nb);
          }
        })
        .catch(function () {
          status.textContent = 'Could not reach the query API. The files above have every row.';
        })
        .then(function () {
          busy = false;
          runB.disabled = false;
        });
    };
    runB.addEventListener('click', function () {
      run(0);
    });
    qc.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && e.target.tagName === 'INPUT' && e.target.type !== 'checkbox') {
        e.preventDefault();
        run(0);
      }
    });
    paintFilters();
    paint();
  }

  // Citation card: four forms of one sentence.
  var citeData = document.getElementById('cite-data');
  if (citeData) {
    var forms = JSON.parse(citeData.textContent);
    document.querySelectorAll('.cite-tabs button').forEach(function (b) {
      b.addEventListener('click', function () {
        document.querySelectorAll('.cite-tabs button').forEach(function (x) {
          x.setAttribute('aria-selected', 'false');
        });
        b.setAttribute('aria-selected', 'true');
        document.getElementById('cite-text').textContent = forms[b.dataset.cite];
        document.getElementById('cite-src').value = forms[b.dataset.cite];
      });
    });
  }

  // The header on a narrow screen: the menu button shows and hides the site links, and Escape
  // closes them and returns focus to the button.
  var menu = document.querySelector('.menu');
  if (menu) {
    var header = menu.closest('header');
    var setMenu = function (on) {
      menu.setAttribute('aria-expanded', on ? 'true' : 'false');
      header.classList.toggle('open', on);
    };
    menu.addEventListener('click', function () {
      setMenu(menu.getAttribute('aria-expanded') !== 'true');
    });
    header.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape' || menu.getAttribute('aria-expanded') !== 'true') {
        return;
      }
      setMenu(false);
      menu.focus();
    });
  }

  // Home search: the cards filter in place as you type, and submit searches the whole catalogue.
  var q = document.getElementById('q'),
    ledger = document.getElementById('ledger');
  if (q && ledger) {
    var cards = ledger.querySelectorAll('[data-card]'),
      none = document.getElementById('ledger-none');
    var more = document.getElementById('ledger-more'),
      all = false;
    var filter = function () {
      var s = q.value.trim().toLowerCase(),
        any = false;
      cards.forEach(function (c) {
        c.hidden = s
          ? c.textContent.toLowerCase().indexOf(s) < 0
          : !all && c.classList.contains('more');
        if (!c.hidden) {
          any = true;
        }
      });
      if (more) {
        more.hidden = !!s || all;
      }
      ledger.querySelectorAll('.collection').forEach(function (g) {
        g.hidden = !g.querySelector('[data-card]:not([hidden])');
      });
      if (none) {
        none.hidden = any;
      }
    };
    q.addEventListener('input', filter);
    if (more) {
      more.addEventListener('click', function () {
        all = true;
        filter();
      });
    }
    var search = new URLSearchParams(location.search);
    if (search.get('q')) {
      q.value = search.get('q');
      filter();
    }
  }

  // Tabs: a row of buttons that each show one pane.
  document.querySelectorAll('[data-tabs]').forEach(function (box) {
    var tabList = box.querySelectorAll('[role=tab]');
    tabList.forEach(function (tab) {
      tab.addEventListener('click', function () {
        tabList.forEach(function (t) {
          var on = t === tab;
          t.setAttribute('aria-selected', on ? 'true' : 'false');
          var pane = document.getElementById(t.getAttribute('aria-controls'));
          if (pane) {
            pane.hidden = !on;
          }
        });
      });
    });
  });

  // Votes: one click, no identity. The server counts one per browser per day.
  var mine = {};
  try {
    mine = JSON.parse(localStorage.getItem('pd-votes') || '{}');
  } catch (e) {
    // Storage is blocked, so votes count without being remembered here.
  }
  var countsP = null;
  function counts() {
    var get = function (u) {
      return fetch(u)
        .then(function (r) {
          return r.ok ? r.json() : {};
        })
        .catch(function () {
          return {};
        });
    };
    // Counts are keyed by register slug; a page built before a record was claimed still names its id.
    if (!countsP) {
      countsP = Promise.all([get('/api/v1/votes'), get('/catalogue/aliases.json')]).then(
        function (a) {
          Object.keys(a[1]).forEach(function (id) {
            if (a[0][a[1][id]] && !(id in a[0])) {
              Object.defineProperty(a[0], id, { value: a[0][a[1][id]], enumerable: false });
            }
          });
          return a[0];
        }
      );
    }
    return countsP;
  }
  function paintVote(b, n, on) {
    b.querySelector('b').textContent = n;
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
    b.querySelector('span').textContent = on ? 'voted' : 'vote';
  }
  function bindVote(b) {
    var k = b.dataset.vote;
    counts().then(function (c) {
      paintVote(b, c[k] || 0, !!mine[k]);
    });
    b.addEventListener('click', function () {
      var on = b.getAttribute('aria-pressed') === 'true';
      fetch('/api/v1/votes/' + encodeURIComponent(k), { method: on ? 'DELETE' : 'POST' })
        .then(function (r) {
          return r.json();
        })
        .then(function (d) {
          if (d.error) {
            say(d.error);
            return;
          }
          if (on) {
            delete mine[k];
          } else {
            mine[k] = 1;
          }
          if (d.slug && d.slug !== k) {
            if (on) {
              delete mine[d.slug];
            } else {
              mine[d.slug] = 1;
            }
          }
          try {
            localStorage.setItem('pd-votes', JSON.stringify(mine));
          } catch (e) {
            // Storage is blocked; the vote is counted all the same.
          }
          paintVote(b, d.votes, !on);
          say(on ? 'Vote removed' : 'Counted. Thank you.');
        })
        .catch(function () {
          say('Could not reach the vote counter');
        });
    });
  }
  document.querySelectorAll('.vote[data-vote]').forEach(bindVote);

  // A table filtered by the words typed above it.
  document.querySelectorAll('input[data-filter]').forEach(function (i) {
    var rows = document.querySelectorAll(i.dataset.filter + ' tbody tr');
    if (i.form) {
      i.form.addEventListener('submit', function (e) {
        e.preventDefault();
      });
    }
    i.addEventListener('input', function () {
      var w = i.value.toLowerCase().trim();
      rows.forEach(function (r) {
        r.hidden = !!w && r.textContent.toLowerCase().indexOf(w) < 0;
      });
    });
  });

  // The catalogue: every dataset on the portals, searched through /api/v1/catalogue.
  var JUR = {
    cth: 'Cth',
    nsw: 'NSW',
    vic: 'Vic',
    qld: 'Qld',
    wa: 'WA',
    sa: 'SA',
    tas: 'Tas',
    act: 'ACT',
    nt: 'NT',
  };
  function elem(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) {
      e.className = cls;
    }
    if (text !== undefined && text !== null) {
      e.textContent = text;
    }
    return e;
  }
  function catRow(r) {
    var tr = elem('tr', r.state === 'closed' ? 'closed' : '');
    var t = elem('td', 't'),
      a = elem('a', '', r.title);
    a.href = r.page || r.url;
    if (!r.page) {
      a.rel = 'nofollow noopener';
    }
    t.appendChild(a);
    if (r.summary) {
      t.appendChild(elem('span', 'sub', r.summary));
    }
    var p = elem('td');
    if (r.publisher_path) {
      var pa = elem('a', '', r.publisher);
      pa.href = r.publisher_path;
      p.appendChild(pa);
    } else {
      p.appendChild(document.createTextNode(r.publisher));
    }
    p.appendChild(document.createTextNode(' '));
    p.appendChild(elem('span', 'chip jur', JUR[r.jur] || r.jur));
    var l = elem('td', 't', r.licence);
    if (r.formats) {
      l.appendChild(elem('span', 'sub', r.formats));
    }
    var v = elem('td');
    if (r.state === 'served') {
      var sv = elem('a', 'chip live', 'served');
      sv.href = r.page;
      v.appendChild(sv);
    } else if (r.state === 'closed') {
      v.appendChild(elem('span', 'why', r.reason));
    } else {
      var b = elem('button', 'vote');
      b.type = 'button';
      b.dataset.vote = r.vote;
      b.innerHTML = '<b>0</b><span>vote</span>';
      v.appendChild(b);
      bindVote(b);
    }
    tr.appendChild(t);
    tr.appendChild(p);
    tr.appendChild(l);
    tr.appendChild(v);
    return tr;
  }
  var cs = document.getElementById('cat-search');
  if (cs) {
    var cres = document.getElementById('cat-results'),
      cbody = cres.querySelector('tbody'),
      cmore = document.getElementById('cat-more'),
      ccount = document.getElementById('cat-count'),
      cnext = null,
      ctext = '';
    var cparams = function (off) {
      var p = new URLSearchParams(),
        words = cs.q.value.trim();
      if (words) {
        p.set('q', words);
      }
      if (cs.jur.value) {
        p.set('jur', cs.jur.value);
      }
      if (cs.open.checked) {
        p.set('state', 'votable,chosen,served');
      }
      if (off) {
        p.set('offset', off);
      }
      return p;
    };
    var crun = function (off) {
      var p = cparams(off);
      try {
        history.replaceState(null, '', location.pathname + (p.toString() ? '?' + cparams(0) : ''));
      } catch (e) {
        // The address bar is a convenience; the search runs without it.
      }
      if (!off) {
        cbody.textContent = '';
      }
      cres.hidden = false;
      ccount.textContent = 'Searching…';
      cmore.hidden = true;
      fetch('/api/v1/catalogue?' + p)
        .then(function (r) {
          return r.json();
        })
        .then(function (d) {
          if (d.error) {
            ccount.textContent = d.error;
            return;
          }
          d.rows.forEach(function (r) {
            cbody.appendChild(catRow(r));
          });
          cnext = d.next_offset;
          cmore.hidden = cnext === null || cnext === undefined;
          if (d.total === undefined) {
            ccount.textContent = ctext;
            return;
          }
          var grey = cbody.querySelector('tr.closed');
          ccount.textContent = d.total
            ? d.total.toLocaleString('en-AU') +
              (d.total === 1 ? ' dataset matches.' : ' datasets match.') +
              (grey
                ? ' Grey rows have no open licence or no file to read, so they cannot take a vote.'
                : '')
            : 'Nothing matches. Try fewer words, or all governments.';
          ctext = ccount.textContent;
        })
        .catch(function () {
          ccount.textContent = 'Could not reach the catalogue. Try again, or browse by government.';
        });
    };
    cs.addEventListener('submit', function (e) {
      e.preventDefault();
      crun(0);
    });
    cmore.addEventListener('click', function () {
      crun(cnext);
    });
    var cu = new URLSearchParams(location.search);
    if (cu.get('q') || cu.get('jur')) {
      cs.q.value = cu.get('q') || '';
      cs.jur.value = cu.get('jur') || '';
      cs.open.checked = !!cu.get('state');
      crun(0);
    }
  }

  // The most-voted datasets, from the register and the catalogue alike. The page's own rows stay
  // until there are votes to show, and then fill the table behind them so it keeps its height. Votes
  // past the page's row count wait behind a button, so the table only grows when a reader asks.
  var mw = document.getElementById('most-wanted');
  if (mw) {
    var mlimit = Number(mw.dataset.limit) || 5,
      mbody = mw.querySelector('tbody'),
      mfirst = [].slice.call(mbody.rows),
      msize = mfirst.length || mlimit;
    Promise.all([
      counts(),
      fetch('/backlog.json')
        .then(function (r) {
          return r.ok ? r.json() : { entries: [] };
        })
        .catch(function () {
          return { entries: [] };
        }),
    ]).then(function (a) {
      var c = a[0],
        reg = {};
      (a[1].entries || []).forEach(function (e) {
        reg[e.slug] = e;
      });
      var keys = Object.keys(c)
        .filter(function (k) {
          var e = reg[k];
          return !e || (e.status !== 'live' && e.status !== 'building');
        })
        .sort(function (x, y) {
          return c[y] - c[x] || (x < y ? -1 : 1);
        })
        .slice(0, mlimit);
      if (!keys.length) {
        return;
      }
      var ids = keys.filter(function (k) {
        return !reg[k];
      });
      var got = ids.length
        ? fetch('/api/v1/catalogue?ids=' + ids.map(encodeURIComponent).join(','))
            .then(function (r) {
              return r.ok ? r.json() : { rows: [] };
            })
            .catch(function () {
              return { rows: [] };
            })
        : Promise.resolve({ rows: [] });
      got.then(function (d) {
        var by = {};
        d.rows.forEach(function (r) {
          by[r.id] = r;
          if (r.vote && !by[r.vote]) {
            by[r.vote] = r;
          }
        });
        // A dataset the page already shows keeps its own row, so its cells and height stay as rendered.
        var own = {},
          rows = [],
          shown = {};
        mfirst.forEach(function (tr) {
          var b = tr.querySelector('[data-vote]');
          if (b) {
            own[b.dataset.vote] = tr;
          }
        });
        keys.forEach(function (k) {
          if (own[k]) {
            rows.push(own[k]);
            shown[k] = 1;
            return;
          }
          var e = reg[k];
          var r = e
            ? {
                title: e.title,
                summary: e.summary,
                publisher: e.publisher.name,
                jur: e.publisher.jurisdiction.toLowerCase(),
                licence: e.licence.title,
                url: e.source,
                state: e.status === 'blocked' ? 'closed' : 'chosen',
                reason: e.blocked_reason,
                vote: k,
              }
            : by[k];
          if (r && r.state !== 'served' && !shown[r.vote || k]) {
            rows.push(catRow(r));
            shown[r.vote || k] = 1;
          }
        });
        if (!rows.length) {
          return;
        }
        var extra = rows.splice(msize);
        mfirst.forEach(function (tr) {
          var b = tr.querySelector('[data-vote]');
          if (rows.length < msize && !(b && shown[b.dataset.vote])) {
            rows.push(tr);
          }
        });
        // A phone fires resize when its address bar hides, so only a change of width lets the table go.
        var mwrap = mw.parentNode,
          mwidth = mwrap.offsetWidth;
        var mwide = function () {
          if (mwrap.offsetWidth !== mwidth) {
            mfree();
          }
        };
        var mfree = function () {
          mwrap.style.minHeight = '';
          window.removeEventListener('resize', mwide);
        };
        mwrap.style.minHeight = mwrap.offsetHeight + 'px';
        window.addEventListener('resize', mwide);
        mbody.textContent = '';
        rows.concat(extra).forEach(function (r) {
          mbody.appendChild(r);
        });
        if (!extra.length) {
          return;
        }
        extra.forEach(function (r) {
          r.hidden = true;
        });
        var mb = elem(
          'button',
          'btn ghost',
          'Show ' + extra.length + ' more ' + (extra.length === 1 ? 'dataset' : 'datasets')
        );
        mb.type = 'button';
        mb.setAttribute('aria-controls', mw.id);
        mb.setAttribute('aria-expanded', 'false');
        var mp = elem('p');
        mp.appendChild(mb);
        mw.parentNode.parentNode.insertBefore(mp, mw.parentNode.nextSibling);
        mb.addEventListener('click', function () {
          mfree();
          extra.forEach(function (r) {
            r.hidden = false;
          });
          mb.setAttribute('aria-expanded', 'true');
          extra[0].querySelector('a, button').focus();
          mp.hidden = true;
        });
      });
    });
  }

  // A pasted portal link becomes a vote for the dataset it names.
  var sug = document.getElementById('suggest');
  if (sug) {
    var sout = document.getElementById('sug-result');
    var tell = function (parts) {
      sout.textContent = '';
      parts.forEach(function (x) {
        if (typeof x === 'string') {
          sout.appendChild(document.createTextNode(x));
        } else {
          var a = elem('a', '', x.text);
          a.href = x.href;
          if (x.ext) {
            a.rel = 'noopener';
          }
          sout.appendChild(a);
        }
      });
      sout.hidden = false;
    };
    sug.addEventListener('submit', function (e) {
      e.preventDefault();
      var i = document.getElementById('sug');
      if (!i.value) {
        say('Paste a dataset URL first');
        return;
      }
      tell(['Looking it up…']);
      fetch('/api/v1/requests', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ url: i.value }),
      })
        .then(function (r) {
          return r.json();
        })
        .then(function (d) {
          var r = d.record;
          if (d.error) {
            tell([d.error]);
          } else if (d.status === 'voted') {
            mine[r.vote] = 1;
            try {
              localStorage.setItem('pd-votes', JSON.stringify(mine));
            } catch (e2) {
              // Storage is blocked; the vote is counted all the same.
            }
            tell([
              'Counted as a vote for ',
              { text: r.title, href: r.url, ext: 1 },
              ' from ' +
                r.publisher +
                '. It has ' +
                d.votes +
                (d.votes === 1 ? ' vote.' : ' votes.'),
            ]);
            i.value = '';
          } else if (d.status === 'served') {
            tell([r.title + ' is already served here. ', { text: 'Open it', href: d.page }]);
          } else if (d.status === 'closed') {
            tell(['We found ' + r.title + ', but it cannot take a vote: ' + d.reason + '.']);
          } else {
            tell([
              'That page is not in the catalogue we read on ' +
                d.catalogue_read +
                '. It may be newer, or on a portal we do not read yet. ',
              { text: 'Send it to National Digital', href: d.contact, ext: 1 },
              ', who run this site, and a person will look at it.',
            ]);
          }
        })
        .catch(function () {
          tell(['Could not reach the backlog. Try again in a moment.']);
        });
    });
  }

  // WebMCP: in-page tools for agents driving a browser that exposes navigator.modelContext.
  // Names, descriptions and schemas come from api.json at build; only the calls live here.
  var mc = navigator.modelContext || document.modelContext;
  if (mc && typeof mc.registerTool === 'function' && SPEC) {
    var W = SPEC.webmcp;
    var dsEl = document.getElementById('ds-data');
    var DS = null;
    try {
      DS = dsEl && JSON.parse(dsEl.textContent);
    } catch (e) {
      // Unreadable page data leaves the tools without a page of their own.
    }
    var PAGE =
      DS && DS.console ? { slug: DS.slug, title: DS.title, fields: DS.console.fields } : null;
    var text = function (v) {
      return typeof v === 'string' ? v : JSON.stringify(v);
    };
    var getJSON = function (u) {
      return fetch(u).then(function (r) {
        if (!r.ok) {
          throw new Error(r.status + ' for ' + u);
        }
        return r.json();
      });
    };
    var catalog = function () {
      return getJSON('/catalog.json');
    };
    var latest = function (slug) {
      return getJSON('/latest.json').then(function (m) {
        if (!m[slug]) {
          throw new Error('no live dataset with slug ' + slug);
        }
        return m[slug];
      });
    };
    var vbase = function (slug) {
      return latest(slug).then(function (v) {
        return '/d/' + encodeURIComponent(slug) + '/v/' + v + '/';
      });
    };
    var partitionIndex = function (slug, field) {
      return vbase(slug).then(function (b) {
        return getJSON(b + 'by/' + encodeURIComponent(field) + '/index.json').then(function (ix) {
          ix.base = b;
          return ix;
        });
      });
    };
    var API = '/api/v1/datasets/';
    var encValue = function (v) {
      return encodeURIComponent(String(v));
    };
    var filters = function (where) {
      var out = [];
      Object.keys(where || {}).forEach(function (k) {
        var w = where[k],
          f = encValue(k);
        if (w === null) {
          out.push(f + '=is.null');
        } else if (Array.isArray(w)) {
          if (
            w.some(function (x) {
              return String(x).indexOf(',') >= 0;
            })
          ) {
            throw new Error(
              'a list value cannot contain a comma; filter ' + k + ' on one value at a time'
            );
          }
          out.push(f + '=in.' + encValue('(' + w.join(',') + ')'));
        } else if (typeof w === 'object') {
          if (w.min !== undefined) {
            out.push(f + '=gte.' + encValue(w.min));
          }
          if (w.max !== undefined) {
            out.push(f + '=lte.' + encValue(w.max));
          }
          if (w.like !== undefined) {
            out.push(f + '=ilike.' + encValue(w.like));
          }
        } else {
          out.push(f + '=eq.' + encValue(w));
        }
      });
      return out;
    };
    var api = function (slug, op, version, qs) {
      var u =
        API +
        encValue(slug) +
        '/' +
        (version ? 'versions/' + encValue(version) + '/' : '') +
        op +
        (qs.length ? '?' + qs.join('&') : '');
      return fetch(u).then(function (r) {
        return r
          .json()
          .catch(function () {
            return {};
          })
          .then(function (b) {
            if (r.status === 429) {
              throw new Error(
                'rate limited; wait ' +
                  (r.headers.get('retry-after') || '10') +
                  ' seconds and call again'
              );
            }
            if (!r.ok) {
              throw new Error(b.error || r.status + ' for ' + u);
            }
            b.version = r.headers.get('x-publicdata-version');
            b.url = location.origin + u;
            return b;
          });
      });
    };
    var total = function (slug, version, where) {
      return api(slug, 'aggregate', version, filters(where)).then(function (b) {
        return b.rows[0] ? b.rows[0].count : 0;
      });
    };
    var slugOf = function (input) {
      var s = input.slug || (PAGE && PAGE.slug);
      if (!s) {
        throw new Error('slug is required; find one with search_datasets');
      }
      return s;
    };

    var JT = { integer: 'integer', number: 'number', boolean: 'boolean' };
    var fieldSchema = function (f) {
      var d = (f.description ? f.description + ' ' : '') + 'Type ' + f.type + '.';
      if (f.min !== undefined) {
        d += ' From ' + f.min + ' to ' + f.max + '.';
      }
      var one = f.values ? { enum: f.values } : { type: JT[f.type] || 'string' };
      return {
        description: d,
        anyOf: [
          one,
          { type: 'array', items: one },
          { type: 'null' },
          {
            type: 'object',
            properties: { min: {}, max: {}, like: { type: 'string' } },
            additionalProperties: false,
          },
        ],
      };
    };
    var whereSchema = function () {
      var s = { type: 'object', description: W.where, additionalProperties: true };
      if (PAGE) {
        s.properties = {};
        PAGE.fields.forEach(function (f) {
          s.properties[f.name] = fieldSchema(f);
        });
      }
      return s;
    };
    var schemaFor = function (t) {
      var props = {};
      Object.keys(t.input).forEach(function (k) {
        var p = t.input[k],
          o = {};
        if (p.api === 'filters') {
          props[k] = whereSchema();
          return;
        }
        Object.keys(p).forEach(function (x) {
          if (x !== 'api') {
            o[x] = p[x];
          }
        });
        if (!o.description && p.api === 'version') {
          o.description = W.version;
        } else if (!o.description && p.api) {
          o.description = SPEC.parameters[p.api].description;
        }
        props[k] = o;
      });
      var req = t.required.slice();
      if (PAGE && t.api && props.slug) {
        props.slug = {
          type: 'string',
          default: PAGE.slug,
          description:
            props.slug.description +
            '. ' +
            W.page_note.replace('{slug}', PAGE.slug).replace('{title}', PAGE.title),
        };
        req = req.filter(function (r) {
          return r !== 'slug';
        });
      }
      return { type: 'object', properties: props, required: req, additionalProperties: false };
    };

    var EXEC = {};
    EXEC.search_datasets = function (input) {
      var s = input.query || '';
      return getJSON('/api/v1/datasets?q=' + encodeURIComponent(s)).then(text, function () {
        return catalog().then(function (c) {
          var l = s.toLowerCase();
          return text({
            results: (c.dataset || [])
              .filter(function (d) {
                return JSON.stringify(d).toLowerCase().indexOf(l) >= 0;
              })
              .map(function (d) {
                return {
                  slug: d.identifier,
                  title: d.title,
                  publisher: d.publisher && d.publisher.name,
                  licence: d.license,
                  page: d.landingPage,
                  latest: d.distribution && d.distribution[0] && d.distribution[0].accessURL,
                };
              }),
          });
        });
      });
    };
    EXEC.get_dataset = function (input) {
      var base = '/d/' + encodeURIComponent(input.slug) + '/';
      return Promise.all([
        getJSON(base + 'datapackage.json'),
        getJSON(base + 'versions.json'),
      ]).then(function (a) {
        return text({ datapackage: a[0], versions: a[1] });
      });
    };
    EXEC.list_fields = function (input) {
      var slug;
      return Promise.resolve()
        .then(function () {
          slug = slugOf(input);
          return getJSON('/d/' + encodeURIComponent(slug) + '/fields.json');
        })
        .then(text, function (e) {
          if (!slug) {
            throw e;
          }
          throw new Error(
            'no queryable dataset with slug ' + slug + '; search_datasets names them'
          );
        });
    };
    EXEC.list_partitions = function (input) {
      return partitionIndex(input.slug, input.field).then(function (ix) {
        return text({
          field: ix.field,
          version_base: location.origin + ix.base,
          partitions: ix.partitions.map(function (e) {
            return { value: e.value, rows: e.rows, url: location.origin + ix.base + e.json };
          }),
        });
      });
    };
    EXEC.query_rows = function (input) {
      var limit = input.limit || 50,
        offset = input.offset || 0,
        slug;
      return Promise.resolve()
        .then(function () {
          slug = slugOf(input);
          var qs = filters(input.where).concat(['limit=' + limit, 'offset=' + offset]);
          if (input.select && input.select.length) {
            qs.push('select=' + input.select.map(encValue).join(','));
          }
          if (input.order) {
            qs.push('order=' + encValue(input.order));
          }
          return api(slug, 'rows', input.version, qs);
        })
        .then(function (b) {
          return total(slug, b.version, input.where).then(function (n) {
            return text({
              version: b.version,
              rows: b.rows,
              matched: n,
              next_offset: b.next ? offset + limit : null,
              query: b.url,
              attribution: b.publicdata && b.publicdata.attribution,
            });
          });
        });
    };
    EXEC.count_rows = function (input) {
      var metric = input.metric || 'count',
        limit = input.limit || 100,
        group = input.group_by || [],
        slug;
      var alias = metric === 'count' ? 'count' : metric.replace('.', '_');
      return Promise.resolve()
        .then(function () {
          slug = slugOf(input);
          var order = [alias + '.desc']
            .concat(
              group.map(function (g) {
                return g + '.asc';
              })
            )
            .map(encValue)
            .join(',');
          var qs = filters(input.where).concat([
            'metric=' + encValue(metric),
            'order=' + order,
            'limit=' + limit,
          ]);
          if (group.length) {
            qs.push('group=' + group.map(encValue).join(','));
          }
          return api(slug, 'aggregate', input.version, qs);
        })
        .then(function (b) {
          return total(slug, b.version, input.where).then(function (n) {
            return text({
              version: b.version,
              group_by: group,
              metric: metric,
              groups: b.rows,
              truncated: !!b.next,
              matched: n,
              query: b.url,
              attribution: b.publicdata && b.publicdata.attribution,
            });
          });
        });
    };
    EXEC.diff_versions = function (input) {
      return getJSON(
        '/d/' + encodeURIComponent(input.slug) + '/diff/' + input.from + '..' + input.to + '.json'
      ).then(text);
    };
    EXEC.search_catalogue = function (input) {
      var qs = new URLSearchParams({ q: input.query || '', limit: '20' });
      if (input.jurisdiction) {
        qs.set('jur', input.jurisdiction);
      }
      if (input.votable_only) {
        qs.set('state', 'votable,chosen');
      }
      if (input.offset) {
        qs.set('offset', String(input.offset));
      }
      return getJSON('/api/v1/catalogue?' + qs).then(text);
    };
    var orNull = function (v) {
      return v === undefined || v === null ? null : v;
    };
    var backlog = function (register, votes) {
      var byVotes = function (a, b) {
        return b.votes - a.votes || ((a.slug || a.vote) < (b.slug || b.vote) ? -1 : 1);
      };
      var known = {};
      register.entries.forEach(function (e) {
        known[e.slug] = 1;
      });
      var entries = register.entries
        .filter(function (e) {
          return e.status !== 'live';
        })
        .map(function (e) {
          return {
            slug: e.slug,
            title: e.title,
            publisher: e.publisher ? e.publisher.name : null,
            status: e.status,
            votes: votes[e.slug] || 0,
            summary: orNull(e.summary),
            source: orNull(e.source),
            blocked_reason: orNull(e.blocked_reason),
          };
        });
      var cv = Object.keys(votes)
        .filter(function (k) {
          return !known[k];
        })
        .map(function (k) {
          return { vote: k, votes: votes[k] };
        });
      return { entries: entries.sort(byVotes), catalogue_votes: cv.sort(byVotes) };
    };
    EXEC.list_backlog = function () {
      return Promise.all([getJSON('/backlog.json'), getJSON('/api/v1/votes')]).then(function (a) {
        return text(backlog(a[0], a[1]));
      });
    };
    EXEC.upvote_dataset = function (input) {
      return fetch('/api/v1/votes/' + encodeURIComponent(input.slug), { method: 'POST' })
        .then(function (r) {
          return r.json();
        })
        .then(text);
    };

    Object.keys(W.tools).forEach(function (name) {
      var t = W.tools[name];
      var d = {
        name: name,
        title: t.title,
        annotations: Object.assign({ title: t.title }, t.annotations),
        description:
          t.description +
          (PAGE && t.api
            ? ' ' + W.page_note.replace('{slug}', PAGE.slug).replace('{title}', PAGE.title)
            : ''),
        inputSchema: schemaFor(t),
        execute: EXEC[name],
      };
      try {
        var p = mc.registerTool(d);
        if (p && p.catch) {
          p.catch(function () {});
        }
      } catch (e) {
        // A tool the browser refuses leaves the others registered.
      }
    });
  }
})();
