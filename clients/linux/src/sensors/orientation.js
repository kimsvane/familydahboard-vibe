'use strict';

const { readAcceleration } = require('./iio');

// Værdierne er de samme navne som kscreen-doctor bruger til rotation, så
// der ikke skal oversættes noget når retningen skal sættes på skærmen.
const NONE = 'none';
const LEFT = 'left';
const RIGHT = 'right';
const INVERTED = 'inverted';

// En voksende enhed ligger næsten helt i én akse. Derfor afgøres retningen af
// den akse der dominerer, i stedet for at regne en skrå vinkel ud.
//
// Margen måles mellem de to stærkeste akser, altså hvor meget den voksende
// akse har over den næstbedste. Står enheden på kanten, hvor de to er næsten
// lige stærke, er der intet tydeligt svar, og så røres skærmen ikke.
const AXIS_MARGIN = 0.3;

/// Hvilken akse og hvilken værdi af den, der afgør en given retning.
/// Det er nødvendigt at kende aksen, fordi opret og omvendt opret begge
/// afgøres af y: de kan ikke skelnes på størrelsen af y, kun på tegnet.
function measure(accel) {
  if (!accel) return null;
  const { x, y, z } = accel;
  if (![x, y, z].every((v) => typeof v === 'number' && Number.isFinite(v))) return null;

  const skala = Math.max(Math.abs(x), Math.abs(y), Math.abs(z));
  if (!(skala > 0)) return null;

  const akser = [
    { axis: 'x', fraction: Math.abs(x) / skala, orientation: x > 0 ? RIGHT : LEFT },
    { axis: 'y', fraction: Math.abs(y) / skala, orientation: y < 0 ? NONE : INVERTED },
    { axis: 'z', fraction: Math.abs(z) / skala, orientation: NONE },
  ].sort((a, b) => b.fraction - a.fraction);

  const [først, anden] = akser;
  if (først.fraction - anden.fraction < AXIS_MARGIN) {
    return { orientation: null, axis: null, fraction: først.fraction, ambiguous: true };
  }
  return { orientation: først.orientation, axis: først.axis, fraction: først.fraction, ambiguous: false };
}

/// Vælger den retning ud fra den akse der bærer tyngdekraften.
function orientationFor(accel) {
  return measure(accel)?.orientation ?? null;
}

/// Holder styr på hvilken retning skærmen står i nu. Accelerometeret ryster,
/// så en ny retning accepteres først når den er klart stærkere end den
/// nuværende. Uden det ville billedet hoppe, mens enheden står på kanten.
class OrientationTracker {
  constructor(options = {}) {
    this.readAcceleration = options.readAcceleration ?? readAcceleration;
    this.current = options.current ?? null;
  }

  /// Den retning der anbefales lige nu, uden at huske noget.
  peek() {
    return orientationFor(this.readAcceleration());
  }

  /// Den retning der bør bruges nu, med hensyn til tidligere aflæsninger.
  read() {
    const accel = this.readAcceleration();
    const målt = measure(accel);
    if (!målt) return this.current;
    // Står enheden lige på kanten, er der intet nyt at skifte til.
    if (målt.ambiguous) return this.current;
    if (this.current === null) {
      this.current = målt.orientation;
      return this.current;
    }
    if (målt.orientation === this.current) return this.current;

    this.current = målt.orientation;
    return this.current;
  }

  /// Den akse en retning hører til. Bruges til at kende et hold fra en
  /// virkelig drejning.
  axisOf(orientation) {
    if (orientation === LEFT || orientation === RIGHT) return 'x';
    return 'y';
  }
}

module.exports = {
  AXIS_MARGIN,
  INVERTED,
  LEFT,
  NONE,
  OrientationTracker,
  RIGHT,
  measure,
  orientationFor,
};
