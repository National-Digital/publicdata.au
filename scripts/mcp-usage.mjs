// Counts the MCP server's usage lines (functions/_usage.js) by class, method, tool and client.
// Pipe a live tail straight in and stop it with Ctrl-C to print the counts:
//   npx wrangler pages deployment tail --project-name publicdata-au --format json | node scripts/mcp-usage.mjs
// Do not save a raw tail to a file: every event in it holds the request's headers and
// Cloudflare's location fields for the caller. A Logpush file of Workers trace events can be
// named as an argument instead. Only the console lines of each event are read.
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

// The JSON values in a text: one per line, or pretty-printed with each value's first line
// unindented, as wrangler prints them. A value cut off by Ctrl-C is skipped and counted.
export function values(text) {
  const out = [];
  let cur = null;
  let skipped = 0;
  const close = () => {
    if (cur) {
      try {
        out.push(JSON.parse(cur.join('\n')));
      } catch {
        skipped++;
      }
      cur = null;
    }
  };
  for (const l of text.split(/\r?\n/)) {
    if (/^[{[]/.test(l)) {
      close();
      try {
        out.push(JSON.parse(l));
      } catch {
        cur = [l];
      }
    } else if (cur && /^[\s}\]]/.test(l)) {
      cur.push(l);
    } else if (l.trim()) {
      close();
      skipped++;
    }
  }
  close();
  return { values: out, skipped };
}

const isLine = (v) => v && typeof v === 'object' && v.event === 'mcp' && v.methods;

// The usage lines an event's console output holds, or the value itself when it is one.
export function lines(v) {
  if (isLine(v)) {
    return [v];
  }
  const logs = (v && (v.logs || v.Logs)) || [];
  const out = [];
  for (const l of Array.isArray(logs) ? logs : []) {
    const msg = l && (l.message || l.Message);
    for (const m of Array.isArray(msg) ? msg : []) {
      let x = m;
      if (typeof m === 'string') {
        try {
          x = JSON.parse(m);
        } catch {
          continue;
        }
      }
      if (isLine(x)) {
        out.push(x);
      }
    }
  }
  return out;
}

export function count(text) {
  const { values: vs, skipped } = values(text);
  const out = {};
  const add = (cls, what, key, n = 1) => {
    out[cls] ||= {};
    out[cls][what] ||= {};
    out[cls][what][key] = (out[cls][what][key] || 0) + n;
  };
  for (const v of vs) {
    for (const l of lines(v)) {
      const cls = l.class === 'probe' ? 'probe' : 'client';
      add(cls, 'request', `${l.http} ${l.status}`);
      for (const [m, n] of Object.entries(l.methods)) {
        add(cls, 'method', m, n);
      }
      for (const [t, n] of Object.entries(l.tools || {})) {
        add(cls, 'tool', t, n);
        add(cls, 'tool call by agent', l.ua || '(none)', n);
      }
      if (l.client) {
        add(cls, 'initialize by client', `${l.client} ${l.client_version || ''}`.trim());
      }
      for (const [e, n] of Object.entries(l.errors || {})) {
        add(cls, 'error', e, n);
      }
    }
  }
  return { counts: out, skipped };
}

export function report({ counts, skipped }) {
  const rows = [];
  for (const cls of ['client', 'probe']) {
    rows.push(cls);
    for (const [what, t] of Object.entries(counts[cls] || {})) {
      rows.push(`  ${what}`);
      for (const [k, n] of Object.entries(t).sort(
        (a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1),
      )) {
        rows.push(`    ${String(n).padStart(6)}  ${k}`);
      }
    }
  }
  rows.push(`skipped ${skipped} input${skipped === 1 ? '' : 's'} that were not whole JSON`);
  return rows.join('\n');
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  const files = process.argv.slice(2);
  const print = (text) => process.stdout.write(report(count(text)) + '\n');
  if (files.length) {
    print(files.map((f) => readFileSync(f, 'utf8')).join('\n'));
  } else {
    const chunks = [];
    const done = () => {
      print(Buffer.concat(chunks).toString('utf8'));
      process.exit(0);
    };
    process.stdin.on('data', (c) => chunks.push(c));
    process.stdin.on('end', done);
    process.on('SIGINT', done);
  }
}
