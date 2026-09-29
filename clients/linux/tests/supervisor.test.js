'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

const { DEFAULTS } = require('../src/power/policy');
const { WallSupervisor } = require('../src/power/supervisor');

function fakeScreen() {
  const calls = { on: 0, off: 0, brightness: [] };
  return {
    calls,
    turnOn(percent, callback) {
      calls.on += 1;
      calls.brightnessOnWake = percent;
      if (typeof callback === 'function') callback();
      else if (typeof percent === 'function') percent();
    },
    turnOff(callback) { calls.off += 1; if (callback) callback(); },
    setBrightnessPercent(percent) { calls.brightness.push(percent); },
    canWriteBrightness() { return true; },
  };
}

function build(settings, options = {}) {
  const screenControl = fakeScreen();
  const events = [];
  let clockMs = 0;
  const supervisor = new WallSupervisor({
    settings: settings ?? DEFAULTS,
    readLight: options.readLight ?? (() => ({ lux: 200 })),
    screenControl,
    pollMs: 10_000_000,
    onSleep: (decision) => events.push(['sleep', decision.reason]),
    onWake: (decision) => events.push(['wake', decision.reason]),
    ...options,
    // Tiden styres manuelt, så dvale-perioden kan testes deterministisk.
    now: () => clockMs,
  });
  return { supervisor, screenControl, events, advance: (ms) => { clockMs += ms; } };
}

test('skærmen er tændt i tidsvinduet og slukket uden for', () => {
  const { supervisor, screenControl } = build();
  const daytime = new Date(2026, 0, 7, 12, 0, 0, 0);
  const night = new Date(2026, 0, 7, 23, 30, 0, 0);

  supervisor.clock = () => daytime;
  supervisor.tick();
  supervisor.clock = () => night;
  supervisor.tick();

  assert.equal(screenControl.calls.off, 1);
  assert.equal(screenControl.calls.on, 0);
});

test('et tryk på skærmen tænder den igen uden for tidsvinduet', () => {
  const { supervisor, screenControl, events } = build();
  supervisor.clock = () => new Date(2026, 0, 7, 23, 30, 0, 0);
  supervisor.tick();
  assert.equal(screenControl.calls.off, 1);

  supervisor.registerTouch();
  assert.equal(screenControl.calls.on, 1);
  assert.equal(screenControl.calls.off, 1);
  assert.deepEqual(events.at(-1), ['wake', 'presence-outside-schedule']);
});

test('et tryk forlænger tiden, så skærmen ikke slukker med det samme', () => {
  const { supervisor, screenControl } = build();
  supervisor.clock = () => new Date(2026, 0, 7, 12, 0, 0, 0);
  supervisor.tick();
  assert.equal(screenControl.calls.off, 0);

  supervisor.registerTouch();
  supervisor.tick();
  assert.equal(screenControl.calls.off, 0);
  assert.equal(supervisor.state.reason, 'warming');
});

test('bevægelse fra Reolink tænder skærmen uden for tidsvinduet', () => {
  const { supervisor, screenControl } = build();
  supervisor.clock = () => new Date(2026, 0, 7, 23, 30, 0, 0);
  supervisor.tick();
  assert.equal(screenControl.calls.off, 1);

  // Et kamera-blink uden varighed giver alligevel en kort præsens-periode,
  // ellers ville skærmen aldrig nå at tænde.
  supervisor.registerExternalPresence(30);
  assert.equal(screenControl.calls.on, 1);
  assert.equal(supervisor.presenceDetected(), true);
});

test('et kamera-blink holder præsens levende i den angivne tid', () => {
  const { supervisor } = build();
  supervisor.clock = () => new Date(2026, 0, 7, 12, 0, 0, 0);
  supervisor.registerExternalPresence(30);
  supervisor.tick();
  assert.equal(supervisor.idleMinutes() < 45, true);
  assert.equal(supervisor.state.on, true);
});

test('lysstyrken følger lyset i rummet når den er tændt', () => {
  const { supervisor, screenControl } = build(DEFAULTS, { readLight: () => ({ lux: 400 }) });
  supervisor.clock = () => new Date(2026, 0, 7, 12, 0, 0, 0);
  supervisor.tick();
  assert.equal(screenControl.calls.brightness.at(-1), 100);
});

test('mørkt rummet dæmper skærmen', () => {
  const { supervisor, screenControl } = build(DEFAULTS, { readLight: () => ({ lux: 0 }) });
  supervisor.clock = () => new Date(2026, 0, 7, 12, 0, 0, 0);
  supervisor.tick();
  assert.equal(screenControl.calls.brightness.at(-1), 15);
});

test('skærmen tændes kun én gang, selv når beslutningen ikke ændrer sig', () => {
  const { supervisor, screenControl } = build();
  supervisor.clock = () => new Date(2026, 0, 7, 12, 0, 0, 0);
  supervisor.tick();
  const afterFirst = screenControl.calls.on;
  supervisor.tick();
  supervisor.tick();
  assert.equal(screenControl.calls.on, afterFirst);
});

test('slukket skærm skærer både tænd- og dæmpningskald', () => {
  const { supervisor, screenControl } = build(DEFAULTS, { readLight: () => ({ lux: 0 }) });
  supervisor.clock = () => new Date(2026, 0, 7, 23, 0, 0, 0);
  supervisor.tick();
  assert.deepEqual(screenControl.calls.brightness, []);
});

test('forceOff slukker skærmen selv om nogen er til stede', () => {
  const { supervisor, screenControl } = build({ ...DEFAULTS, mode: 'always' });
  supervisor.clock = () => new Date(2026, 0, 7, 12, 0, 0, 0);
  supervisor.tick();
  assert.equal(screenControl.calls.on + screenControl.calls.off, 0);

  supervisor.registerTouch();
  supervisor.setForcedOff(true);
  assert.equal(screenControl.calls.off, 1);
  assert.equal(supervisor.state.reason, 'forced-off');

  supervisor.setForcedOff(false);
  assert.equal(screenControl.calls.on, 1);
});

test('tilstanden kan læses til indstillingssiden', () => {
  const { supervisor } = build();
  supervisor.clock = () => new Date(2026, 0, 7, 12, 0, 0, 0);
  supervisor.registerTouch();
  const snapshot = supervisor.tick() && supervisor.snapshot({ lux: 120 });
  assert.equal(snapshot.present, true);
  assert.equal(snapshot.ambientLux, 120);
  assert.equal(snapshot.reason, 'warming');
});

test('en manglende lyssensor giver ingen dæmpning frem for en fejl', () => {
  const { supervisor } = build(DEFAULTS, { readLight: () => null });
  supervisor.clock = () => new Date(2026, 0, 7, 12, 0, 0, 0);
  assert.doesNotThrow(() => supervisor.tick());
  assert.equal(supervisor.state.on, true);
  assert.equal(supervisor.state.brightness, 100);
});

test('skærmen går i dvale selv om ingen nogensinde har rørt den', () => {
  // Regression: uden en starttidspunkt var "tid siden sidste præsens" 0 lige
  // meget, så varm-up-perioden aldrig udløb og skærmen blev tændt for evigt.
  const { supervisor, screenControl, advance } = build();
  supervisor.clock = () => new Date(2026, 0, 7, 12, 0, 0, 0);
  supervisor.tick();
  assert.equal(screenControl.calls.off, 0);
  assert.equal(supervisor.state.reason, 'warming');

  advance(46 * 60_000);
  supervisor.tick();
  assert.equal(supervisor.state.reason, 'idle-timeout');
  assert.equal(screenControl.calls.off, 1);
  assert.equal(supervisor.state.on, false);
});

test('et tryk genstarter den fulde dvale-periode', () => {
  const { supervisor, screenControl, advance } = build();
  supervisor.clock = () => new Date(2026, 0, 7, 12, 0, 0, 0);
  supervisor.tick();

  advance(30 * 60_000);
  supervisor.registerTouch();
  assert.equal(supervisor.state.on, true);
  assert.equal(supervisor.state.reason, 'warming');

  advance(30 * 60_000);
  supervisor.tick();
  assert.equal(supervisor.state.on, true, '30 minutter efter et tryk er den stadig tændt');
  assert.equal(screenControl.calls.off, 0);
});

test('en bruger der går og kommer holder skærmen i live', () => {
  const { supervisor, screenControl, advance } = build();
  supervisor.clock = () => new Date(2026, 0, 7, 12, 0, 0, 0);
  for (let time = 0; time < 6 * 60; time += 20) {
    advance(20 * 60_000);
    supervisor.registerTouch();
  }
  supervisor.tick();
  assert.equal(supervisor.state.on, true);
  assert.equal(screenControl.calls.off, 0);
});

test('præsenstilstand slukker, når alle går hjem', () => {
  const { supervisor, advance } = build({ ...DEFAULTS, mode: 'presence' });
  supervisor.clock = () => new Date(2026, 0, 7, 12, 0, 0, 0);
  supervisor.registerTouch();
  supervisor.tick();
  assert.equal(supervisor.state.on, true);

  // Præsens-udlejen fra trykket udløber, og intet nyt registrerer sig.
  advance(4 * 60_000);
  supervisor.tick();
  assert.equal(supervisor.presenceDetected(), false);
  assert.equal(supervisor.state.on, false);
  assert.equal(supervisor.state.reason, 'no-presence');
});

test('ét tryk holder ikke skærmen tændt for evigt', () => {
  // Regression: tilstedeværelse var en kontakt der blev stående, så ét tryk
  // gjorde at skærmen aldrig nåede dvale-tiden.
  const { supervisor, advance } = build();
  supervisor.clock = () => new Date(2026, 0, 7, 12, 0, 0, 0);
  supervisor.registerTouch();
  supervisor.tick();
  assert.equal(supervisor.state.on, true);

  advance(20 * 60_000);
  supervisor.tick();
  assert.equal(supervisor.presenceDetected(), false, 'præsens-udlejen er udløbet');
  assert.equal(supervisor.state.on, true, 'men dvale-tiden er ikke nået endnu');

  advance(30 * 60_000);
  supervisor.tick();
  assert.equal(supervisor.state.on, false, 'efter 50 minutter går den i dvale');
  assert.equal(supervisor.state.reason, 'idle-timeout');
});

test('gentagne tryk forlænger lejen og holder skærmen tændt', () => {
  const { supervisor, advance } = build();
  supervisor.clock = () => new Date(2026, 0, 7, 12, 0, 0, 0);
  for (let minute = 0; minute < 90; minute += 20) {
    advance(20 * 60_000);
    supervisor.registerTouch();
  }
  supervisor.tick();
  assert.equal(supervisor.state.on, true);
});

test('stop afbryder overvågningen, så processen kan afslutte rent', () => {
  const { supervisor } = build();
  supervisor.start();
  assert.notEqual(supervisor.timer, null);
  supervisor.stop();
  assert.equal(supervisor.timer, null);
});

test('skærmen vækker med den lysstyrke der passer til rummet, ikke med fuld styrke', () => {
  const { supervisor, screenControl } = build();
  supervisor.clock = () => new Date(2026, 0, 7, 23, 30, 0, 0);
  supervisor.readLight = () => ({ lux: 5 });
  supervisor.tick();
  assert.equal(screenControl.calls.off, 1, 'skærmen skal være slukket først');

  supervisor.registerTouch();
  assert.equal(screenControl.calls.on, 1);
  assert.notEqual(screenControl.calls.brightnessOnWake, 100, 'et mørkt rum må ikke give fuld lysstyrke');
  assert.ok(
    screenControl.calls.brightnessOnWake >= 15 && screenControl.calls.brightnessOnWake <= 100,
    `lysstyrken ${screenControl.calls.brightnessOnWake} skal ligge i det tilladte interval`,
  );
});

test('vågning i et lyst rum må gerne give fuld lysstyrke', () => {
  const { supervisor, screenControl } = build();
  supervisor.clock = () => new Date(2026, 0, 7, 23, 30, 0, 0);
  supervisor.readLight = () => ({ lux: 5000 });
  supervisor.tick();
  assert.equal(screenControl.calls.off, 1);

  supervisor.registerTouch();
  assert.equal(screenControl.calls.on, 1);
  assert.equal(screenControl.calls.brightnessOnWake, 100);
});

test('bevægelse fra kameraet vækker med samme dæmpede lysstyrke', () => {
  const { supervisor, screenControl } = build();
  supervisor.clock = () => new Date(2026, 0, 7, 23, 30, 0, 0);
  supervisor.readLight = () => ({ lux: 5 });
  supervisor.tick();
  assert.equal(screenControl.calls.off, 1);

  supervisor.registerExternalPresence(30);
  assert.equal(screenControl.calls.on, 1);
  assert.notEqual(screenControl.calls.brightnessOnWake, 100);
});

function roter(setup = {}) {
  const fake = fakeScreen();
  fake.rotation = [];
  fake.setRotation = (name, callback) => {
    fake.rotation.push(name);
    if (setup.fail) callback?.(new Error('nægter'));
    else callback?.(null);
  };
  return { fake };
}

function medRotation(indstillinger, setup = {}) {
  const { fake } = roter(setup);
  const supervisor = new WallSupervisor({
    settings: { ...DEFAULTS, ...indstillinger },
    screenControl: fake,
    clock: () => new Date(2026, 0, 7, 12, 0, 0, 0),
    readLight: () => ({ lux: 200 }),
    readAcceleration: () => setup.accel ?? { x: 0, y: 0, z: 1 },
  });
  return { supervisor, fake };
}

const VENSTRE = { x: -1, y: 0, z: 0 };
const HOEJRE = { x: 1, y: 0, z: 0 };
const FLAD = { x: 0, y: 0, z: 1 };

test('skærmen følger hældningen når rotation er auto', () => {
  const { supervisor, fake } = medRotation({ display: { rotation: 'auto' } }, { accel: VENSTRE });
  supervisor.tick();
  assert.deepEqual(fake.rotation, ['left']);
});

test('samme retning to gange drejer kun én gang', () => {
  const { supervisor, fake } = medRotation({ display: { rotation: 'auto' } }, { accel: VENSTRE });
  supervisor.tick();
  supervisor.tick();
  supervisor.tick();
  assert.deepEqual(fake.rotation, ['left'], 'hver drejning får billedet til at blinke');
});

test('skærmen drejer igen når hældningen ændrer sig', () => {
  let accel = FLAD;
  const { fake } = roter();
  const supervisor = new WallSupervisor({
    settings: { ...DEFAULTS, display: { rotation: 'auto' } },
    screenControl: fake,
    clock: () => new Date(2026, 0, 7, 12, 0, 0, 0),
    readLight: () => ({ lux: 200 }),
    readAcceleration: () => accel,
  });
  supervisor.tick();
  accel = HOEJRE;
  supervisor.tick();
  assert.deepEqual(fake.rotation, ['none', 'right']);
});

test('statisk rotation låser retningen, uanset hældningen', () => {
  const { supervisor, fake } = medRotation(
    { display: { rotation: 'static', staticRotation: 'inverted' } },
    { accel: VENSTRE },
  );
  supervisor.tick();
  assert.deepEqual(fake.rotation, ['inverted']);
});

test('statisk rotation uden valgt vinkel bruger landskab', () => {
  const { supervisor, fake } = medRotation({ display: { rotation: 'static' } });
  supervisor.tick();
  assert.deepEqual(fake.rotation, ['none']);
});

test('er rotation ikke sat, røres skærmen ikke', () => {
  const { fake } = roter();
  const supervisor = new WallSupervisor({
    settings: { ...DEFAULTS, display: undefined },
    screenControl: fake,
    clock: () => new Date(2026, 0, 7, 12, 0, 0, 0),
    readLight: () => ({ lux: 200 }),
    readAcceleration: () => VENSTRE,
  });
  supervisor.tick();
  assert.deepEqual(fake.rotation, [], 'skærmen må ikke drejes uden at det er bedt om');
});

test('er skærmen låst fra hånden, skal den kunne komme tilbage til samme retning', () => {
  // Efter en fejl må den næste forsøg igen prøve, ellers bliver billedet
  // fast i en forkert retning.
  const { supervisor, fake } = medRotation({ display: { rotation: 'auto' } }, { accel: VENSTRE, fail: true });
  supervisor.tick();
  supervisor.tick();
  assert.deepEqual(fake.rotation, ['left', 'left'], 'den skal prøve igen efter en fejl');
});

test(' naar enheden ligger paa kanten, gaar skoermen tilbage til landskab', () => {
  // Ellers kunne den blive haengende i en tidligere valgt retning, og sa
  // kunne den kun frigores ved at vride enheden.
  const { supervisor, fake } = medRotation({ display: { rotation: 'auto' } }, { accel: { x: 1, y: 1, z: 0 } });
  supervisor.tick();
  assert.deepEqual(fake.rotation, ['none']);
});

test(' en tidligere laaset retning springes ikke over ved omstilling til auto', () => {
  const { fake } = roter();
  let accel = { x: 1, y: 0, z: 0 };
  const supervisor = new WallSupervisor({
    settings: { ...DEFAULTS, display: { rotation: 'static', staticRotation: 'inverted' } },
    screenControl: fake,
    clock: () => new Date(2026, 0, 7, 12, 0, 0, 0),
    readLight: () => ({ lux: 200 }),
    readAcceleration: () => accel,
  });
  supervisor.tick();
  assert.deepEqual(fake.rotation, ['inverted']);

  supervisor.settings = { ...DEFAULTS, display: { rotation: 'auto' } };
  supervisor.tick();
  assert.deepEqual(fake.rotation, ['inverted', 'right']);
  accel = VENSTRE;
  supervisor.tick();
  assert.deepEqual(fake.rotation, ['inverted', 'right', 'left']);
});
