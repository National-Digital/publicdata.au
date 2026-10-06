import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { disposition, downloadName } from './_download.js';

const CASES = JSON.parse(readFileSync(new URL('../pipeline/tests/fixtures/download_names.json', import.meta.url)));

test('a file is named the way site.download_name names it', () => {
  for (const c of CASES) assert.equal(downloadName(c.slug, c.version, c.rel), c.name, c.rel);
});

test('only a file inside a dated version gets a file name', () => {
  assert.equal(disposition('d/x/v/2026-04-24/source.xlsx'), 'inline; filename="x_2026-04-24_source.xlsx"');
  assert.equal(disposition('d/x/v/2026-04-24/index.html'), null);
  assert.equal(disposition('d/x/v/2026-04-24/'), null);
  assert.equal(disposition('d/x/latest/data.csv'), null);
  assert.equal(disposition('d/x/versions.json'), null);
});
