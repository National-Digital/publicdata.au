import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';

// Runs the script in a throwaway repository, so the tags it reads are the ones set here.
const script = new URL('./next-version.mjs', import.meta.url).pathname;
const repo = mkdtempSync(join(tmpdir(), 'next-version-'));
const git = (...a) => execFileSync('git', ['-C', repo, ...a], { stdio: 'pipe' });
git('init', '-q');
git(
  '-c',
  'user.name=t',
  '-c',
  'user.email=t@example.com',
  'commit',
  '-q',
  '--allow-empty',
  '-m',
  'chore: init',
);
const next = (...a) =>
  execFileSync('node', [script, ...a], {
    cwd: repo,
    stdio: ['ignore', 'pipe', 'ignore'],
  }).toString();

test('with no tag the floor is 1.0.0', () => {
  assert.equal(next('--print-current'), '1.0.0');
  assert.equal(next('feat: x'), '1.1.0');
});

test('the newest tag wins, and the title picks the bump', () => {
  for (const t of ['v1.2.9', 'v1.10.0', 'v0.9.0', 'not-a-version']) git('tag', t);
  assert.equal(next('--print-current'), '1.10.0');
  assert.equal(next('feat(agents): x'), '1.11.0');
  assert.equal(next('fix: y'), '1.10.1');
  assert.equal(next('data: new source versions'), '1.10.1');
  assert.equal(next('feat!: drop the v1 paths'), '2.0.0');
  assert.equal(next('refactor: x'), '1.10.1');
  assert.equal(next('Merge something'), '1.10.1');
});

test('with no subject it reads the last commit', () => {
  assert.equal(next(), '1.10.1');
});
