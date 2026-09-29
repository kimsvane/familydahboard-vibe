'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { DEFAULTS } = require('../src/power/policy');
const { mergeWallSettings, readWallSettings, wallPath, writeWallSettings } = require('../src/settings');

function tempDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'fd-wall-'));
}

test('manglende vægfil giver standardindstillingerne', () => {
  const dir = tempDir();
  assert.deepEqual(readWallSettings(dir, DEFAULTS), DEFAULTS);
});

test('gemte indstillinger kan læses tilbage', () => {
  const dir = tempDir();
  writeWallSettings(dir, DEFAULTS, { mode: 'presence', schedule: { from: '07:00', to: '21:00' } });
  const read = readWallSettings(dir, DEFAULTS);
  assert.equal(read.mode, 'presence');
  assert.equal(read.schedule.from, '07:00');
  assert.equal(read.schedule.to, '21:00');
});

test('ubeskrevne felter bevares, når kun en del af et afsnit ændres', () => {
  const dir = tempDir();
  writeWallSettings(dir, DEFAULTS, { brightness: { auto: false } });
  const read = readWallSettings(dir, DEFAULTS);
  assert.equal(read.brightness.auto, false);
  assert.equal(read.brightness.maxPercent, DEFAULTS.brightness.maxPercent);
});

test('vægindstillinger ligger i deres egen fil, ikke i serveropsætningen', () => {
  const dir = tempDir();
  writeWallSettings(dir, DEFAULTS, { mode: 'always' });
  assert.equal(wallPath(dir), path.join(dir, 'wall.json'));
  assert.equal(fs.existsSync(path.join(dir, 'client.json')), false);
});

test('filen skrives med kun rettigheder for ejeren', () => {
  const dir = tempDir();
  writeWallSettings(dir, DEFAULTS, { mode: 'always' });
  const mode = fs.statSync(wallPath(dir)).mode & 0o777;
  assert.equal(mode & 0o077, 0);
});

test('en beskadiget vægfil stopper ikke appen i at starte', () => {
  const dir = tempDir();
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(wallPath(dir), '{ dette er ikke json');
  assert.deepEqual(readWallSettings(dir, DEFAULTS), DEFAULTS);
});

test('en vægfil med en liste i stedet for et objekt ignoreres', () => {
  const dir = tempDir();
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(wallPath(dir), '[1, 2, 3]');
  assert.deepEqual(readWallSettings(dir, DEFAULTS), DEFAULTS);
});

test('felter med forkert type springes over i stedet for at ødelægge resten', () => {
  const dir = tempDir();
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(wallPath(dir), JSON.stringify({ mode: 42, schedule: 'nope' }));
  const read = readWallSettings(dir, DEFAULTS);
  assert.equal(read.mode, DEFAULTS.mode);
  assert.deepEqual(read.schedule, DEFAULTS.schedule);
});

test('null-værdier overskrives ikke', () => {
  const dir = tempDir();
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(wallPath(dir), JSON.stringify({ mode: null }));
  assert.equal(readWallSettings(dir, DEFAULTS).mode, DEFAULTS.mode);
});

test('dagslisten bevares, fordi den er et felt i sig selv', () => {
  const dir = tempDir();
  const dage = [1, 2, 3, 4, 5];
  writeWallSettings(dir, DEFAULTS, { schedule: { days: dage } });
  assert.deepEqual(readWallSettings(dir, DEFAULTS).schedule.days, dage);
});

test('mergeWallSettings lader ukendte nøgler ligge ude', () => {
  const merged = mergeWallSettings({ a: 1 }, { a: 2, ukendt: 3 });
  assert.deepEqual(merged, { a: 2 });
});

test('gemning og læsning er stabil gentagne gange', () => {
  const dir = tempDir();
  let current = DEFAULTS;
  for (const mode of ['presence', 'schedule', 'always']) {
    current = writeWallSettings(dir, DEFAULTS, { ...current, mode });
    assert.equal(readWallSettings(dir, DEFAULTS).mode, mode);
  }
});
