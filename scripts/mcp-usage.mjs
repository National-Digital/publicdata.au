// Counts the MCP server's usage lines (functions/_usage.js) by class, method, tool and client.
// It reads the JSON that `wrangler pages deployment tail --format json` prints, one Logpush file
// of Workers trace events, or the lines alone, from the files named or from stdin. Usage:
//   npx wrangler pages deployment tail --project-name publicdata-au --format json > tail.json
//   node scripts/mcp-usage.mjs tail.json
// A handshake (initialize, tools/list) with no tools/call after it is what a registry checker
// sends, so tools/call by client is the number to read as use.
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

// The JSON values in a text, whether one per line or pretty-printed back to back.
export function* values(text) {
  let depth = 0;
  let start = -1;
  let inString = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inString) {
      if (c === '\\') {
        i++;
      } else if (c === '"') {
        inString = false;
      }
    } else if (c === '"') {
      inString = true;
    } else if (c === '{' || c === '[') {
      if (depth++ === 0) {
        start = i;
      }
    } else if ((c === '}' || c === ']') && depth > 0 && --depth === 0) {
      try {
        yield JSON.parse(text.slice(start, i + 1));
      } catch {
        // A cut-off event at the end of a tail.
      }
    }
  }
}

// Every usage line inside a value, as an object or as the string console.log wrote.
export function* lines(v) {
  if (typeof v === 'string') {
    if (v.startsWith('{"event":"mcp"')) {
      try {
        yield JSON.parse(v);
      } catch {
        // Not one of ours.
      }
    }
  } else if (Array.isArray(v)) {
    for (const x of v) {
      yield* lines(x);
    }
  } else if (v && typeof v === 'object') {
    if (v.event === 'mcp' && typeof v.class === 'string') {
      yield v;
      return;
    }
    for (const x of Object.values(v)) {
      yield* lines(x);
    }
  }
}

export function count(text) {
  const out = {};
  const add = (cls, what, key) => {
    out[cls] ||= {};
    out[cls][what] ||= {};
    out[cls][what][key] = (out[cls][what][key] || 0) + 1;
  };
  for (const v of values(text)) {
    for (const l of lines(v)) {
      const who = l.client ? `${l.client} ${l.client_version || ''}`.trim() : l.ua || '(none)';
      add(l.class, 'method', l.rpc || `${l.http} ${l.status}`);
      if (l.tool) {
        add(l.class, 'tool', l.tool);
        add(l.class, 'tool call by agent', l.ua || '(none)');
      }
      if (l.rpc === 'initialize') {
        add(l.class, 'initialize by client', who);
      }
    }
  }
  return out;
}

export function report(counts) {
  const rows = [];
  for (const cls of ['client', 'probe']) {
    rows.push(`${cls}`);
    for (const [what, t] of Object.entries(counts[cls] || {})) {
      rows.push(`  ${what}`);
      for (const [k, n] of Object.entries(t).sort(
        (a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1),
      )) {
        rows.push(`    ${String(n).padStart(6)}  ${k}`);
      }
    }
  }
  return rows.join('\n');
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  const files = process.argv.slice(2);
  const text = files.length
    ? files.map((f) => readFileSync(f, 'utf8')).join('\n')
    : readFileSync(0, 'utf8');
  process.stdout.write(report(count(text)) + '\n');
}
