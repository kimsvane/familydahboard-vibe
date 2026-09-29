'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const iio = require('../src/sensors/iio');

// Spejler strukturen fra den virkelige Surface Pro 4: fire iio-devices hvor
// den ene er lyssensoren, en accelerometer, et gyroskop og rotation.
function makeFixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fd-iio-'));
  const write = (device, name, files) => {
    const dir = path.join(root, device);
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, 'name'), name);
    for (const [file, value] of Object.entries(files)) {
      fs.writeFileSync(path.join(dir, file), value);
    }
  };
  write('iio:device0', 'dev_rotation', {});
  write('iio:device1', 'als', {
    in_illuminance_raw: '20',
    in_illuminance_scale: '1.000000000',
    in_intensity_both_raw: '4',
    in_intensity_scale: '1.000000000',
  });
  // Den virkelige maskine har gyro-enheden uden nogen kanaler.
  write('iio:device2', 'gyro_3d', {});
  write('iio:device3', 'accel_3d', {
    in_accel_x_raw: '4',
    in_accel_y_raw: '-723',
    in_accel_z_raw: '-640',
    in_accel_scale: '0.009806650',
  });
  return root;
}

test('findDevice finder lyssensoren på navnet, ikke på indeks', () => {
  const root = makeFixture();
  assert.ok(iio.findDevice('als', root).endsWith('iio:device1'));
  assert.equal(iio.findDevice('presence', root), null);
});

test('readAmbientLight gør rå værdi om til lux', () => {
  const value = iio.readAmbientLight(makeFixture());
  assert.equal(value.lux, 20);
  assert.equal(value.intensity, 4);
});

test('readAmbientLight regnerer skala ind, så lux bliver rigtig', () => {
  const root = makeFixture();
  fs.writeFileSync(path.join(root, 'iio:device1', 'in_illuminance_raw'), '1000');
  fs.writeFileSync(path.join(root, 'iio:device1', 'in_illuminance_scale'), '0.01');
  assert.equal(iio.readAmbientLight(root).lux, 10);
});

test('intensity læses fra in_intensity_both_raw, som maskinen faktisk bruger', () => {
  assert.equal(iio.readAmbientLight(makeFixture()).intensity, 4);
});

test('readAmbientLight bruger intensity, når illuminance mangler', () => {
  const root = makeFixture();
  fs.rmSync(path.join(root, 'iio:device1', 'in_illuminance_raw'));
  const value = iio.readAmbientLight(root);
  assert.equal(value.lux, null);
  assert.equal(value.intensity, 4);
});

test('readAcceleration læser alle tre akser i m/s² med maskinens egen skala', () => {
  const value = iio.readAcceleration(makeFixture());
  assert.ok(Math.abs(value.x - 0.0392) < 1e-4);
  assert.ok(Math.abs(value.y + 7.0903) < 1e-4);
  assert.ok(Math.abs(value.z + 6.2762) < 1e-4);
});

test('gyro uden kanaler giver null, selv om enheden findes', () => {
  assert.equal(iio.readGyro(makeFixture()), null);
  assert.equal(iio.findDevice('gyro_3d', makeFixture()) !== null, true);
});

test('describeSensors melder ikke gyro som virkende, når der ingen kanaler er', () => {
  assert.deepEqual(iio.describeSensors(makeFixture()), { light: true, motion: true, gyro: false });
});

test('manglende sensorer giver null i stedet for at kaste', () => {
  assert.equal(iio.readAmbientLight('/findes-ikke'), null);
  assert.equal(iio.readAcceleration('/findes-ikke'), null);
  assert.equal(iio.readGyro('/findes-ikke'), null);
  assert.deepEqual(iio.describeSensors('/findes-ikke'), { light: false, motion: false, gyro: false });
});

test('beskadigede værdier læses som null', () => {
  const root = makeFixture();
  fs.writeFileSync(path.join(root, 'iio:device1', 'in_illuminance_raw'), 'ikke-et-tal');
  assert.equal(iio.readAmbientLight(root).lux, null);
});

test('readScale bruger 1 når skala er 0, så lux ikke bliver NaN', () => {
  const root = makeFixture();
  fs.writeFileSync(path.join(root, 'iio:device1', 'in_illuminance_scale'), '0');
  assert.equal(iio.readAmbientLight(root).lux, 20);
});
