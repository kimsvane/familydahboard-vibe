'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { readSettings, settingsPath, writeSettings } = require('../src/settings');

function temporaryDirectory(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'family-dashboard-kiosk-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return directory;
}

test('returns null when the kiosk has not been configured', (t) => {
  assert.equal(readSettings(temporaryDirectory(t)), null);
});

test('stores and reads a normalized server address', (t) => {
  const directory = temporaryDirectory(t);
  const written = writeSettings(directory, { serverUrl: 'truenas:8080' });
  assert.equal(written.serverUrl, 'http://truenas:8080/');
  assert.deepEqual(readSettings(directory), written);
  assert.equal(fs.statSync(settingsPath(directory)).mode & 0o777, 0o600);
});

test('rejects a corrupt configuration instead of guessing', (t) => {
  const directory = temporaryDirectory(t);
  fs.writeFileSync(settingsPath(directory), 'not json');
  assert.throws(() => readSettings(directory), /beskadiget/);
});

test('rejects a configuration with a missing address', (t) => {
  const directory = temporaryDirectory(t);
  fs.writeFileSync(settingsPath(directory), '{}');
  assert.throws(() => readSettings(directory), /beskadiget/);
});
