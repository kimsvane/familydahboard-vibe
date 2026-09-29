'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { execFile } = require('node:child_process');

const BACKLIGHT_ROOT = '/sys/class/backlight';

function backlightDevice(root = BACKLIGHT_ROOT) {
  let entries;
  try {
    entries = fs.readdirSync(root);
  } catch (error) {
    return null;
  }
  // Foretræ intel_backlight, som er den Surface Pro 4 bruger.
  const preferred = entries.find((name) => name === 'intel_backlight') || entries[0];
  return preferred ? path.join(root, preferred) : null;
}

function brightnessRange(device = backlightDevice()) {
  if (!device) return null;
  try {
    const max = Number(fs.readFileSync(path.join(device, 'max_brightness'), 'utf8').trim());
    if (!Number.isFinite(max) || max <= 0) return null;
    return { max };
  } catch (error) {
    return null;
  }
}

function readBrightness(device = backlightDevice()) {
  const range = brightnessRange(device);
  if (!range) return null;
  try {
    const value = Number(fs.readFileSync(path.join(device, 'brightness'), 'utf8').trim());
    if (!Number.isFinite(value)) return null;
    return { value, ...range, percent: Math.round((value / range.max) * 100) };
  } catch (error) {
    return null;
  }
}

function canWriteBrightness(device = backlightDevice()) {
  if (!device) return false;
  try {
    fs.accessSync(path.join(device, 'brightness'), fs.constants.W_OK);
    return true;
  } catch (error) {
    return false;
  }
}

/// Sætter lysstyrken i procent af maksimum. Værdien klemmes, så et ugyldigt
/// procenttal aldrig kan slukke skærmen permanent ved en fejl.
function setBrightnessPercent(percent, device = backlightDevice()) {
  const range = brightnessRange(device);
  if (!range) return false;
  const clamped = Math.max(0, Math.min(100, Math.round(Number(percent) || 0)));
  try {
    fs.writeFileSync(path.join(device, 'brightness'), String(Math.round((clamped / 100) * range.max)));
    return true;
  } catch (error) {
    return false;
  }
}

function kscreen(args, callback) {
  execFile('kscreen-doctor', args, { timeout: 5000 }, (error) => callback(error));
}

function displayOutputs(callback) {
  execFile('kscreen-doctor', ['-o'], { timeout: 5000 }, (error, stdout) => {
    if (error) {
      callback([]);
      return;
    }
    const outputs = [];
    for (const match of String(stdout).matchAll(/^Output:\s*\d+\s+(\S+)/gm)) {
      outputs.push(match[1]);
    }
    callback(outputs);
  });
}

function primaryOutput(callback) {
  displayOutputs((outputs) => callback(outputs[0] || null));
}

/// Slår selve skærmen fra med DPMS. Det er strømbesparende, og i modsætning
/// til at sænke lysstyrken til nul går billedet ikke bare til sort.
function setDisplayEnabled(enabled, callback) {
  primaryOutput((output) => {
    if (!output) {
      callback(new Error('Ingen skærm blev fundet af kscreen-doctor.'));
      return;
    }
    kscreen([`output.${output}.enable`, enabled ? '1' : '0'], callback);
  });
}

/// Slukker skærmen. Lysstyrken sættes også til nul, fordi nogle drivere
/// gendanner den gamle værdi, når panelet tændes igen.
function turnOff(callback) {
  setBrightnessPercent(0);
  setDisplayEnabled(false, (error) => callback(error || null));
}

function turnOn(callback) {
  setDisplayEnabled(true, () => {
    setBrightnessPercent(100);
    callback(null);
  });
}

module.exports = {
  BACKLIGHT_ROOT,
  backlightDevice,
  brightnessRange,
  canWriteBrightness,
  displayOutputs,
  primaryOutput,
  readBrightness,
  setBrightnessPercent,
  setDisplayEnabled,
  turnOff,
  turnOn,
};
