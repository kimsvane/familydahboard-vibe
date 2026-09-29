'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { normalizeServerUrl } = require('./url');

function settingsPath(userDataDir) {
  return path.join(userDataDir, 'client.json');
}

/// Væg-tilstandens indstillinger bor i deres egen fil, så en beskadiget
/// serveropsætning ikke sletter dem, og en ny firmware kan tilføje felter
/// uden at ændre opsætningsformatet.
function wallPath(userDataDir) {
  return path.join(userDataDir, 'wall.json');
}

function readWallSettings(userDataDir, defaults) {
  let stored;
  try {
    stored = JSON.parse(fs.readFileSync(wallPath(userDataDir), 'utf8'));
  } catch (error) {
    if (error.code === 'ENOENT') {
      return { ...defaults };
    }
    // En beskadiget vægfil må aldrig stoppe dashboardet i at starte.
    return { ...defaults };
  }
  if (!stored || typeof stored !== 'object' || Array.isArray(stored)) {
    return { ...defaults };
  }
  return mergeWallSettings(defaults, stored);
}

function mergeWallSettings(defaults, stored) {
  const merged = { ...defaults };
  for (const [key, value] of Object.entries(stored ?? {})) {
    if (value === undefined || value === null) continue;
    const fallback = defaults?.[key];
    if (fallback && typeof fallback === 'object' && !Array.isArray(fallback) && typeof value === 'object' && !Array.isArray(value)) {
      merged[key] = { ...fallback, ...value };
    } else if (typeof value === typeof fallback) {
      merged[key] = value;
    }
  }
  return merged;
}

function writeWallSettings(userDataDir, defaults, value) {
  const merged = mergeWallSettings(defaults, value);
  fs.mkdirSync(userDataDir, { recursive: true, mode: 0o700 });
  const file = wallPath(userDataDir);
  const temporary = `${file}.tmp`;
  fs.writeFileSync(temporary, `${JSON.stringify(merged, null, 2)}\n`, { mode: 0o600 });
  fs.renameSync(temporary, file);
  return merged;
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

module.exports = {
  mergeWallSettings,
  readSettings,
  readWallSettings,
  settingsPath,
  wallPath,
  writeSettings,
  writeWallSettings,
};
