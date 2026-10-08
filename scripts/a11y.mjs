// Runs axe-core over built pages in both colour schemes and fails on any WCAG 2.2 AA
// violation. Usage: node scripts/a11y.mjs <dist> [path ...]. With no paths, a fixed set of
// representative pages is checked. Chrome is found on PATH or at CHROME_PATH.
import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { extname, join, normalize } from 'node:path';
import puppeteer from 'puppeteer-core';

const require = createRequire(import.meta.url);
const AXE = require.resolve('axe-core/axe.min.js');
const TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice'];
const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css',
  '.js': 'text/javascript',
  '.mjs': 'text/javascript',
  '.json': 'application/json',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.woff2': 'font/woff2',
  '.md': 'text/markdown',
};
const DEFAULT = [
  '/',
  '/backlog/',
  '/browse/',
  '/agents/',
  '/publishers/',
  '/about/',
  '/terms/',
  '/privacy/',
  '/topics/roads/',
  '/d/qld-road-crash-locations/',
  '/d/qld-road-crash-locations/explore/',
  '/d/qld-road-crash-locations/in/gold-coast-city/',
  '/d/gnaf/',
  '/government/',
  '/c/qld-road-crashes/',
  '/qld/',
];

const [dist, ...paths] = process.argv.slice(2);
if (!dist) {
  console.error('usage: node scripts/a11y.mjs <dist> [path ...]');
  process.exit(2);
}
const pages = paths.length ? paths : DEFAULT;

const server = createServer(async (req, res) => {
  let p = decodeURIComponent(new URL(req.url, 'http://x').pathname);
  if (p.endsWith('/')) {
    p += 'index.html';
  }
  const file = normalize(join(dist, p));
  try {
    if (!file.startsWith(normalize(dist))) {
      throw new Error('outside');
    }
    const s = await stat(file);
    if (s.isDirectory()) {
      res.writeHead(301, { location: p + '/' });
      res.end();
      return;
    }
    res.writeHead(200, { 'content-type': TYPES[extname(file)] || 'application/octet-stream' });
    res.end(await readFile(file));
  } catch {
    res.writeHead(404);
    res.end('not found');
  }
});
await new Promise((r) => {
  server.listen(0, '127.0.0.1', r);
});
const base = `http://127.0.0.1:${server.address().port}`;

const browser = await puppeteer.launch({
  executablePath: process.env.CHROME_PATH || '/usr/bin/google-chrome',
  args: ['--no-sandbox', '--disable-gpu'],
});
const axe = await readFile(AXE, 'utf8');
let failed = 0;
try {
  for (const scheme of ['light', 'dark']) {
    for (const path of pages) {
      const page = await browser.newPage();
      await page.setViewport({ width: 1280, height: 900 });
      await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: scheme }]);
      const resp = await page.goto(base + path, { waitUntil: 'networkidle0', timeout: 60000 });
      if (!resp || resp.status() !== 200) {
        console.log(`✗ ${path} (${scheme}): HTTP ${resp ? resp.status() : 'none'}`);
        failed++;
        await page.close();
        continue;
      }
      await page.evaluate(axe);
      const result = await page.evaluate(
        // Runs in the page, so it reaches the page's globals through globalThis.
        (tags) =>
          globalThis.axe.run(globalThis.document, { runOnly: { type: 'tag', values: tags } }),
        TAGS,
      );
      const bad = result.violations.filter(
        (v) =>
          ['serious', 'critical'].includes(v.impact) || v.tags.some((t) => t.startsWith('wcag')),
      );
      if (bad.length) {
        failed++;
        console.log(`✗ ${path} (${scheme}): ${bad.length} rule(s)`);
        for (const v of bad) {
          console.log(`  ${v.id} [${v.impact}] ${v.help}`);
          for (const n of v.nodes.slice(0, 5)) {
            console.log(`    ${n.target.join(' ')}`);
            console.log(`      ${n.failureSummary.split('\n').join(' ').slice(0, 300)}`);
          }
        }
      } else {
        console.log(`✓ ${path} (${scheme})`);
      }
      await page.close();
    }
  }
} finally {
  await browser.close();
  server.close();
}
process.exit(failed ? 1 : 0);
