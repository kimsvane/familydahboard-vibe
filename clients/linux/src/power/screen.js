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

// kscreen-doctor farver sit output, så tallene ligger i escape-sekvenser.
const ANSI = /\x1b\[[0-9;]*m/g;

function stripAnsi(value) {
  return String(value).replace(ANSI, '');
}

function kscreen(args, callback) {
  // Både fejl og svar skal videregives, ellers kan intet læses tilbage.
  execFile('kscreen-doctor', args, { timeout: 5000 }, (error, stdout) => callback(error, stdout));
}

// De fire rotationer kscreen-doctor forstår. Værdierne er bitflag, så det
// er ikke tal der skal sammenlignes direkte.
const ROTATIONS = Object.freeze({ none: 1, left: 2, inverted: 4, right: 8 });

function readRotation(callback) {
  kscreen(['-o'], (error, stdout) => {
    if (error) {
      callback(null);
      return;
    }
    const match = stripAnsi(stdout).match(/Rotation:\s*(\d+)/);
    if (!match) {
      callback(null);
      return;
    }
    const flag = Number(match[1]);
    const name = Object.keys(ROTATIONS).find((key) => ROTATIONS[key] === flag) ?? null;
    callback(name);
  });
}

/// Bygger kommandoen til at tænde eller slukke skærmen.
/// Der hænger ingen værdi på kommandoen: kscreen-doctor kender 'enable' og
/// 'disable', men afviser 'enable 1' med "Unable to parse arguments".
function buildDisplayCommand(output, enabled) {
  return [`output.${output}.${enabled ? 'enable' : 'disable'}`];
}

/// Bygger kommandoen til at dreje skærmen. Rotation er en navngiven værdi,
/// så det hedder output.eDP-1.rotation.left og ikke output.eDP-1.rotate 2.
function buildRotationCommand(output, name) {
  if (!Object.prototype.hasOwnProperty.call(ROTATIONS, name)) return null;
  return [`output.${output}.rotation.${name}`];
}

function setRotation(name, callback = () => {}) {
  const args = buildRotationCommand(ROTATION_OUTPUT, name);
  if (!args) {
    callback(new Error(`Ukendt rotation: ${name}`));
    return;
  }
  kscreen(args, (error) => callback(error ?? null));
}

const DRM_ROOT = '/sys/class/drm';

// Sættes når skærmen er fundet, så kommandoerne ikke skal lede hver gang.
let ROTATION_OUTPUT = 'eDP-1';

/// Finder de tilsluttede skærme gennem kernen. Det er den eneste kilde der
/// altid svarer, også når kscreen-doctor er tavs, og derfor slås den først.
function connectedOutputs(root = DRM_ROOT) {
  let entries;
  try {
    entries = fs.readdirSync(root);
  } catch (error) {
    return [];
  }
  const outputs = [];
  for (const entry of entries) {
    // card1-eDP-1 -> eDP-1. Kort- og stiknumre er ikke det samme.
    const name = entry.replace(/^card\d+-/, '');
    try {
      if (fs.readFileSync(path.join(root, entry, 'status'), 'utf8').trim() === 'connected') {
        outputs.push(name);
      }
    } catch (error) {
      // En stik der ikke kan læses, tæller ikke som tilsluttet.
    }
  }
  // Det indbyggede panel skal altid være først.
  return outputs.sort((a, b) => (a.startsWith('eDP') ? -1 : b.startsWith('eDP') ? 1 : 0));
}

function displayOutputs(callback) {
  // Via eksporten, så kilden kan udskiftes i test.
  const fromKernel = module.exports.connectedOutputs();
  if (fromKernel.length > 0) {
    callback(fromKernel);
    return;
  }
  execFile('kscreen-doctor', ['-o'], { timeout: 5000 }, (error, stdout) => {
    if (error) {
      callback([]);
      return;
    }
    const outputs = [];
    for (const match of stripAnsi(stdout).matchAll(/Output:\s*\d+\s+(\S+)/g)) {
      outputs.push(match[1]);
    }
    callback(outputs);
  });
}

function primaryOutput(callback) {
  displayOutputs((outputs) => {
    const valgt = outputs[0] || null;
    if (valgt) ROTATION_OUTPUT = valgt;
    callback(valgt);
  });
}

/// Slår selve skærmen fra med DPMS. Det er strømbesparende, og i modsætning
/// til at sænke lysstyrken til nul går billedet ikke bare til sort.
function setDisplayEnabled(enabled, callback) {
  primaryOutput((output) => {
    if (!output) {
      callback(new Error('Ingen skærm blev fundet af kscreen-doctor.'));
      return;
    }
    kscreen(buildDisplayCommand(output, enabled), callback);
  });
}

/// Slukker skærmen. Lysstyrken sættes også til nul, fordi nogle drivere
/// gendanner den gamle værdi, når panelet tændes igen.
/// Slukker skærmen. DPMS er forsøgt først, fordi det også gør billedet
/// aktivt sort, men hvis kscreen-doctor ikke svarer, så er lysstyrken nul
/// nok til at panelet går mørkt. Fejl her skal aldrig stoppe resten.
function turnOff(callback, device = backlightDevice()) {
  // Gør en ventende tændning ugyldig, så den ikke tænder igen bagefter.
  taendringsrunde += 1;
  setBrightnessPercent(0, device);
  setDisplayEnabled(false, () => callback(null));
}

// Tæller op, så en forsinket finishing ikke kan tænde skærmen igen efter
// at den er blevet slukket. Uden dette kunne et tryk på "Sluk nu" kort
// efter en værkning blive overskrevet af den ventende tændning.
let taendringsrunde = 0;

/// Tænder skærmen igen. Lysstyrken sættes til den værdi, der er valgt for
/// det lys der er i rummet, og ikke automatisk til hundrede.
function turnOn(percent, callback, device = backlightDevice()) {
  const runde = (taendringsrunde += 1);
  const finish = () => {
    // Er der slået fra i mellemtiden, må denne tændning ikke gennemføres.
    if (runde !== taendringsrunde) {
      callback(null);
      return;
    }
    setBrightnessPercent(Number.isFinite(percent) ? percent : 100, device);
    callback(null);
  };
  setDisplayEnabled(true, finish);
  // DPMS kan hænge. Skærmen må ikke blive sort, fordi et værktøj svarer
  // for langsomt, så lysstyrken sættes uafhængigt af svaret.
  setTimeout(finish, 3000);
}

module.exports = {
  BACKLIGHT_ROOT,
  DRM_ROOT,
  ROTATIONS,
  backlightDevice,
  buildDisplayCommand,
  buildRotationCommand,
  connectedOutputs,
  readRotation,
  setRotation,
  stripAnsi,
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
