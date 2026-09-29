'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const screen = require('../src/power/screen');

function makeBacklight(max = '7500') {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fd-backlight-'));
  fs.mkdirSync(path.join(root, 'intel_backlight'));
  fs.writeFileSync(path.join(root, 'intel_backlight', 'max_brightness'), max);
  fs.writeFileSync(path.join(root, 'intel_backlight', 'brightness'), '5000');
  return path.join(root, 'intel_backlight');
}

test('foretrukker intel_backlight når flere baggrunde findes', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fd-bl2-'));
  fs.mkdirSync(path.join(root, 'acpi_video0'));
  fs.writeFileSync(path.join(root, 'acpi_video0', 'max_brightness'), '15');
  fs.mkdirSync(path.join(root, 'intel_backlight'));
  fs.writeFileSync(path.join(root, 'intel_backlight', 'max_brightness'), '7500');
  fs.writeFileSync(path.join(root, 'intel_backlight', 'brightness'), '5000');
  assert.equal(screen.backlightDevice(root), path.join(root, 'intel_backlight'));
});

test('manglende backlight giver null i stedet for at kaste', () => {
  assert.equal(screen.backlightDevice('/findes-ikke'), null);
  assert.equal(screen.readBrightness('/findes-ikke'), null);
  assert.equal(screen.brightnessRange('/findes-ikke'), null);
  assert.equal(screen.canWriteBrightness('/findes-ikke'), false);
});

test('læser lysstyrke og regner procent ud', () => {
  const value = screen.readBrightness(makeBacklight('7500'));
  assert.equal(value.max, 7500);
  assert.equal(value.value, 5000);
  assert.equal(value.percent, 67);
});

test('skriver lysstyrke som procent af maksimum', () => {
  const device = makeBacklight('7500');
  assert.equal(screen.setBrightnessPercent(50, device), true);
  assert.equal(fs.readFileSync(path.join(device, 'brightness'), 'utf8'), '3750');
  assert.equal(screen.setBrightnessPercent(100, device), true);
  assert.equal(fs.readFileSync(path.join(device, 'brightness'), 'utf8'), '7500');
});

test('procenttal uden for interval klemmes, så skærmen ikke kan låses sort', () => {
  const device = makeBacklight('7500');
  screen.setBrightnessPercent(500, device);
  assert.equal(fs.readFileSync(path.join(device, 'brightness'), 'utf8'), '7500');
  screen.setBrightnessPercent(-50, device);
  assert.equal(fs.readFileSync(path.join(device, 'brightness'), 'utf8'), '0');
});

test('ugyldigt procenttal fører til 0 i stedet for NaN', () => {
  const device = makeBacklight('7500');
  screen.setBrightnessPercent('meget mørkt', device);
  assert.equal(fs.readFileSync(path.join(device, 'brightness'), 'utf8'), '0');
});

test('maksimal værdi 0 gør at intet kan styres', () => {
  const device = makeBacklight('0');
  assert.equal(screen.brightnessRange(device), null);
  assert.equal(screen.setBrightnessPercent(50, device), false);
});

test('beskadiget brightness-fil læses som null', () => {
  const device = makeBacklight('7500');
  fs.writeFileSync(path.join(device, 'brightness'), 'helt i vildt');
  assert.equal(screen.readBrightness(device), null);
});

test('en skrivebeskyttet brightness-fil meldes som ikke skrivbar', { skip: process.getuid?.() === 0 ? 'root kan altid skrive' : false }, () => {
  const device = makeBacklight('7500');
  fs.chmodSync(path.join(device, 'brightness'), 0o444);
  assert.equal(screen.canWriteBrightness(device), false);
  fs.chmodSync(path.join(device, 'brightness'), 0o644);
  assert.equal(screen.canWriteBrightness(device), true);
});

test('skrivning til en skrivebeskyttet enhed fejler rent', { skip: process.getuid?.() === 0 ? 'root kan altid skrive' : false }, () => {
  const device = makeBacklight('7500');
  fs.chmodSync(path.join(device, 'brightness'), 0o444);
  assert.equal(screen.setBrightnessPercent(50, device), false);
});
