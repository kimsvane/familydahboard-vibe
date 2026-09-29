'use strict';

const fs = require('node:fs');
const path = require('node:path');

const IIO_ROOT = '/sys/bus/iio/devices';

function listDevices(root = IIO_ROOT) {
  let entries;
  try {
    entries = fs.readdirSync(root);
  } catch (error) {
    return [];
  }
  return entries
    .filter((name) => name.startsWith('iio:device'))
    .map((name) => path.join(root, name));
}

function deviceName(device) {
  try {
    return fs.readFileSync(path.join(device, 'name'), 'utf8').trim();
  } catch (error) {
    return '';
  }
}

function findDevice(name, root = IIO_ROOT) {
  return listDevices(root).find((device) => deviceName(device) === name) || null;
}

function readText(file) {
  try {
    return fs.readFileSync(file, 'utf8').trim();
  } catch (error) {
    return null;
  }
}

function readNumber(file) {
  const text = readText(file);
  if (text === null) return null;
  const value = Number(text);
  return Number.isFinite(value) ? value : null;
}

/// Finder en værdi uanset hvad den hedder. Surface Pro 4 bruger blandt andet
/// in_intensity_both_raw, så både den og den korte form skal prøves.
function readChannel(device, field) {
  const names = [`in_${field}_raw`, `in_${field}_both_raw`, `in_${field}`, `in_${field}_both`];
  for (const name of names) {
    const value = readNumber(path.join(device, name));
    if (value !== null) return value;
  }
  return null;
}

function readScale(device, field) {
  const value = readNumber(path.join(device, `in_${field}_scale`));
  return value === null || value === 0 ? 1 : value;
}

/// Læser lyssensoren og returnerer lux. Surface Pro 4 leverer både
/// in_illuminance_raw og in_intensity_raw; den første er den kalibrerede lux.
function readAmbientLight(root = IIO_ROOT) {
  const device = findDevice('als', root);
  if (!device) return null;
  const scale = readScale(device, 'illuminance');
  const raw = readChannel(device, 'illuminance');
  const intensityRaw = readChannel(device, 'intensity');
  const lux = raw !== null ? raw * scale : null;
  if (lux === null && intensityRaw === null) return null;
  return {
    lux,
    intensity: intensityRaw === null ? null : intensityRaw * readScale(device, 'intensity'),
  };
}

/// Læser accelerometeren i m/s². Bruges til at mærke at nogen rører skærmen
/// eller bærer den, hvilket er et godt supplement til kameraet.
function readAcceleration(root = IIO_ROOT) {
  const device = findDevice('accel_3d', root);
  if (!device) return null;
  const scale = readScale(device, 'accel');
  const axes = { x: readChannel(device, 'accel_x'), y: readChannel(device, 'accel_y'), z: readChannel(device, 'accel_z') };
  if (axes.x === null && axes.y === null && axes.z === null) return null;
  return {
    x: axes.x === null ? null : axes.x * scale,
    y: axes.y === null ? null : axes.y * scale,
    z: axes.z === null ? null : axes.z * scale,
  };
}

function readGyro(root = IIO_ROOT) {
  const device = findDevice('gyro_3d', root);
  if (!device) return null;
  const scale = readScale(device, 'angvel');
  const axes = { x: readChannel(device, 'angvel_x'), y: readChannel(device, 'angvel_y'), z: readChannel(device, 'angvel_z') };
  if (axes.x === null && axes.y === null && axes.z === null) return null;
  return {
    x: axes.x === null ? null : axes.x * scale,
    y: axes.y === null ? null : axes.y * scale,
    z: axes.z === null ? null : axes.z * scale,
  };
}

/// En sensor tæller kun som tilgængelig, hvis den faktisk har en kanal at læse.
/// På Surface Pro 4 findes gyro-enheden, men den har ingen in_angvel-kanaler,
/// så den er ubrugelig og skal ikke meldes som virkende.
function describeSensors(root = IIO_ROOT) {
  return {
    light: readAmbientLight(root) !== null,
    motion: readAcceleration(root) !== null,
    gyro: readGyro(root) !== null,
  };
}

module.exports = {
  IIO_ROOT,
  describeSensors,
  findDevice,
  listDevices,
  readAcceleration,
  readAmbientLight,
  readChannel,
  readGyro,
  readNumber,
  readScale,
  readText,
};
