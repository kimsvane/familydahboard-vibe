'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { normalizeServerUrl } = require('./url');

function settingsPath(userDataDir) {
  return path.join(userDataDir, 'client.json');
}

function readSettings(userDataDir) {
  const file = settingsPath(userDataDir);
  let stored;
  try {
    stored = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (error) {
    if (error.code === 'ENOENT') {
      return null;
    }
    throw new Error('Opsætningsfilen er beskadiget. Indtast serveradressen igen.');
  }
  if (!stored || typeof stored.serverUrl !== 'string') {
    throw new Error('Opsætningsfilen er beskadiget. Indtast serveradressen igen.');
  }
  return { serverUrl: normalizeServerUrl(stored.serverUrl) };
}

function writeSettings(userDataDir, value) {
  const serverUrl = normalizeServerUrl(value?.serverUrl);
  fs.mkdirSync(userDataDir, { recursive: true, mode: 0o700 });
  const file = settingsPath(userDataDir);
  const temporary = `${file}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify({ serverUrl }, null, 2)}\n`, { mode: 0o600 });
  fs.renameSync(temporary, file);
  return { serverUrl };
}

module.exports = { readSettings, settingsPath, writeSettings };
