'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

const {
  INVERTED,
  LEFT,
  NONE,
  OrientationTracker,
  RIGHT,
  measure,
  orientationFor,
} = require('../src/sensors/orientation');

test('liggende fladt er landskab', () => {
  assert.equal(orientationFor({ x: 0, y: 0, z: 1 }), NONE);
});

test('liggende fladt med negativ z er også landskab', () => {
  assert.equal(orientationFor({ x: 0, y: 0, z: -1 }), NONE);
});

test('opret i landskab er ingen rotation', () => {
  assert.equal(orientationFor({ x: 0, y: -1, z: 0 }), NONE);
});

test('opret på hovedet er 180 grader', () => {
  assert.equal(orientationFor({ x: 0, y: 1, z: 0 }), INVERTED);
});

test('på siden mod uret skal billedet drejes til højre', () => {
  assert.equal(orientationFor({ x: 1, y: 0, z: 0 }), RIGHT);
});

test('på siden med uret skal billedet drejes til venstre', () => {
  assert.equal(orientationFor({ x: -1, y: 0, z: 0 }), LEFT);
});

test('en næsten flad enhed tæller som flad', () => {
  assert.equal(orientationFor({ x: 0.2, y: 0.1, z: 0.95 }), NONE);
});

test('en næsten opret enhed tæller som opret', () => {
  assert.equal(orientationFor({ x: 0.15, y: -0.97, z: 0.18 }), NONE);
});

test('alle nuller giver ingen retning', () => {
  assert.equal(orientationFor({ x: 0, y: 0, z: 0 }), null);
});

test('manglende data giver ingen retning', () => {
  assert.equal(orientationFor(null), null);
  assert.equal(orientationFor({ x: 1, y: null, z: 1 }), null);
  assert.equal(orientationFor({ x: NaN, y: 1, z: 1 }), null);
});

test('målingen fortæller hvilken akse der afgør retningen', () => {
  assert.equal(measure({ x: 0, y: -1, z: 0 }).axis, 'y');
  assert.equal(measure({ x: 1, y: 0, z: 0 }).axis, 'x');
  assert.equal(measure({ x: 0, y: 0, z: 1 }).axis, 'z');
});

test('målingen giver en andel mellem nul og én', () => {
  const m = measure({ x: 0.2, y: -0.9, z: 0.3 });
  assert.ok(m.fraction > 0.5 && m.fraction <= 1);
});

test('målingen er tom uden data', () => {
  assert.equal(measure(null), null);
  assert.equal(measure({ x: 0, y: 0, z: 0 }), null);
});

function tracker(række) {
  let i = 0;
  return new OrientationTracker({ readAcceleration: () => række[Math.min(i++, række.length - 1)] });
}

test('første aflæsning låser retningen', () => {
  const t = tracker([{ x: 0, y: -1, z: 0 }]);
  assert.equal(t.read(), NONE);
  assert.equal(t.current, NONE);
});

test('en lille rysten skifter ikke retning', () => {
  const t = tracker([{ x: 0, y: -1, z: 0 }, { x: -0.62, y: -0.6, z: 0.1 }]);
  assert.equal(t.read(), NONE);
  assert.equal(t.read(), NONE, 'en lille hældning må ikke dreje skærmen');
});

test('en tydelig lægning drejer til venstre', () => {
  const t = tracker([{ x: 0, y: -1, z: 0 }, { x: -1, y: 0, z: 0 }]);
  assert.equal(t.read(), NONE);
  assert.equal(t.read(), LEFT);
});

test('en hældning lige mellem to akser beholder den gamle retning', () => {
  const t = tracker([{ x: 0, y: -1, z: 0 }, { x: 0.6, y: 0.6, z: 0.1 }]);
  assert.equal(t.read(), NONE);
  assert.equal(t.read(), NONE, 'to akser der er lige stærke er ikke et tydeligt signal');
});

test('målingen melder en uafgjort stilling', () => {
  const m = measure({ x: 0.6, y: 0.6, z: 0.1 });
  assert.equal(m.ambiguous, true);
  assert.equal(m.orientation, null);
});

test('en tydelig hældning mod én akse er ikke længere uafgjort', () => {
  const m = measure({ x: -0.95, y: -0.3, z: 0.05 });
  assert.equal(m.ambiguous, false);
  assert.equal(m.orientation, LEFT);
});

test('lægges enheden helt flad, går den tilbage til landskab', () => {
  const t = tracker([{ x: -1, y: 0, z: 0 }, { x: 0, y: 0, z: 1 }]);
  assert.equal(t.read(), LEFT);
  assert.equal(t.read(), NONE);
});

test('op og ned ad stillingen følger med', () => {
  const t = tracker([{ x: 0, y: -1, z: 0 }, { x: 0, y: 1, z: 0 }]);
  assert.equal(t.read(), NONE);
  assert.equal(t.read(), INVERTED);
});

test('en tracker uden sensor beholder den forrige retning', () => {
  const t = tracker([{ x: 0, y: -1, z: 0 }, null]);
  assert.equal(t.read(), NONE);
  assert.equal(t.read(), NONE);
});

test('peek påvirker ikke den gemte retning', () => {
  const t = tracker([{ x: -1, y: 0, z: 0 }]);
  assert.equal(t.peek(), LEFT);
  assert.equal(t.current, null);
});

test('en valgfri startretning gælder, når sensoren ikke svarer', () => {
  const t = new OrientationTracker({ current: INVERTED, readAcceleration: () => null });
  assert.equal(t.read(), INVERTED);
  assert.equal(t.current, INVERTED);
});

test('sensoren må gerne overskrive en valgfri startretning', () => {
  const t = new OrientationTracker({ current: INVERTED, readAcceleration: () => ({ x: 0, y: -1, z: 0 }) });
  assert.equal(t.read(), NONE, 'sensoren er kilden til sandheden');
});

test('x overtager y når hældningen bliver tydelig nok', () => {
  const række = [{ x: 0, y: -1, z: 0 }, { x: -0.95, y: -0.3, z: 0.05 }];
  let i = 0;
  const t = new OrientationTracker({ readAcceleration: () => række[Math.min(i++, 1)] });
  assert.equal(t.read(), NONE);
  assert.equal(t.read(), LEFT);
});
