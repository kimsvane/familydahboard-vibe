'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

const { DEFAULTS, brightnessFor, decide, inWindow, parseTime } = require('../src/power/policy');

function at(hours, minutes = 0) {
  const date = new Date(2026, 0, 7, hours, minutes, 0, 0);
  return date;
}

function withSettings(overrides) {
  return { ...DEFAULTS, ...overrides };
}

const insideWindow = at(12, 0);
const outsideWindow = at(23, 0);

test('parseTime læser HH:MM og afviser umulige værdier', () => {
  assert.equal(parseTime('06:30'), 390);
  assert.equal(parseTime('6:30'), 390);
  assert.equal(parseTime('22:30'), 1350);
  assert.equal(parseTime('24:00'), null);
  assert.equal(parseTime('12:99'), null);
  assert.equal(parseTime('nonsense'), null);
  assert.equal(parseTime(undefined), null);
});

test('tidsvindue der krydser midnat regnes som aktivt klokken 23:30', () => {
  const night = { schedule: { enabled: true, from: '22:00', to: '06:00' } };
  assert.equal(decide(night, { now: at(23, 30) }).on, true);
  assert.equal(decide(night, { now: at(2, 0) }).on, true);
  assert.equal(decide(night, { now: at(7, 0) }).on, false);
  assert.equal(inWindow(1320, 360, 1410), true);
  assert.equal(inWindow(1320, 360, 120), true);
  assert.equal(inWindow(1320, 360, 420), false);
  assert.equal(inWindow(390, 1350, 720), true);
  assert.equal(inWindow(390, 1350, 1380), false);
});

test('schedule-tilstand: tændt i vinduet, slukket uden for', () => {
  assert.equal(decide(undefined, { now: insideWindow }).on, true);
  assert.equal(decide(undefined, { now: outsideWindow }).on, false);
});

test('slukket uden for tidsplanen med en grund', () => {
  const result = decide(undefined, { now: outsideWindow });
  assert.equal(result.on, false);
  assert.equal(result.brightness, 0);
  assert.equal(result.reason, 'outside-schedule');
});

test('tilstedeværelse vækker skærmen uden for tidsplanen', () => {
  const result = decide(withSettings({ presence: { ...DEFAULTS.presence, wakeOutsideSchedule: true } }), {
    now: outsideWindow,
    presenceDetected: true,
  });
  assert.equal(result.on, true);
  assert.equal(result.reason, 'presence-outside-schedule');
});

test('vågning uden for tidsplanen kan slås fra', () => {
  const result = decide(withSettings({ presence: { ...DEFAULTS.presence, wakeOutsideSchedule: false } }), {
    now: outsideWindow,
    presenceDetected: true,
  });
  assert.equal(result.on, false);
});

test('præsenstilstand lyser kun med nogen i nærheden, uanset tidsplan', () => {
  const presenceOnly = withSettings({ mode: 'presence' });
  assert.equal(decide(presenceOnly, { now: at(12, 0), presenceDetected: false }).on, false);
  assert.equal(decide(presenceOnly, { now: at(3, 0), presenceDetected: true }).on, true);
});

test('præsenstilstand slår tidsplanen helt fra', () => {
  const presenceOnly = withSettings({ mode: 'presence' });
  const midtOmNatten = decide(presenceOnly, { now: at(2, 0), presenceDetected: false });
  assert.equal(midtOmNatten.on, false);
  assert.equal(midtOmNatten.reason, 'no-presence');
});

test('går i dvale efter 45 minutter uden tegn på liv i tidsvinduet', () => {
  const result = decide(undefined, { now: insideWindow, presenceDetected: false, idleMinutes: 46 });
  assert.equal(result.on, false);
  assert.equal(result.reason, 'idle-timeout');
});

test('varm-up holder skærmen tændt de første minutter efter vækning', () => {
  const result = decide(undefined, { now: insideWindow, presenceDetected: false, idleMinutes: 1 });
  assert.equal(result.on, true);
  assert.equal(result.reason, 'warming');
});

test('sluk aldrig med det samme igen lige efter opvågning', () => {
  for (const idle of [0, 1, 2]) {
    assert.equal(decide(undefined, { now: insideWindow, presenceDetected: false, idleMinutes: idle }).on, true);
  }
  assert.equal(decide(undefined, { now: insideWindow, presenceDetected: false, idleMinutes: 3 }).on, true);
});

test('tilstedeværelse holder skærmen tændt uanset hvor længe den har været stille', () => {
  const result = decide(undefined, { now: insideWindow, presenceDetected: true, idleMinutes: 600 });
  assert.equal(result.on, true);
});

test('deaktiveret præsenssensor gør at tomgangs-timeren holdes ude', () => {
  const off = withSettings({ presence: { ...DEFAULTS.presence, enabled: false } });
  const result = decide(off, { now: insideWindow, presenceDetected: false, idleMinutes: 600 });
  assert.equal(result.on, true);
});

test('always-tilstand tænder uanset alt', () => {
  const always = withSettings({ mode: 'always' });
  assert.equal(decide(always, { now: at(3, 0), presenceDetected: false }).on, true);
});

test('forcedOff har altid sidste ord, også når nogen er til stede', () => {
  const result = decide(withSettings({ mode: 'always' }), { now: at(12, 0), presenceDetected: true, forcedOff: true });
  assert.equal(result.on, false);
  assert.equal(result.reason, 'forced-off');
});

test('lysstyrken følger lyset i rummet', () => {
  const auto = { brightness: { auto: true, darkLux: 5, brightLux: 400, minPercent: 15, maxPercent: 100 } };
  assert.equal(brightnessFor(auto, 5), 15);
  assert.equal(brightnessFor(auto, 400), 100);
  assert.equal(brightnessFor(auto, 202.5), 58);
});

test('ekstremt mørkt eller lyst bliver klemt til det tilladte interval', () => {
  const auto = { brightness: { auto: true, darkLux: 5, brightLux: 400, minPercent: 15, maxPercent: 100 } };
  assert.equal(brightnessFor(auto, 0), 15);
  assert.equal(brightnessFor(auto, 99999), 100);
});

test('slukket lyssensor dæmmer for et mørkt rum i stedet for at blende', () => {
  // Der er valgt det modsatte tidligere. Det er rigtigt at ville gætte
  // naer sensoren tier, men fuld lysstyrke er netop det forkerte gæt:
  // vaegget sidder paa en vaeg i sovevaerrelset og skal ikke skinne i
  // hovedet paa nogen, fordi et sensorproblem er opstaet.
  assert.equal(brightnessFor({ brightness: { auto: true, minPercent: 15, maxPercent: 100 } }, null), 15);
});

test('manuel lysstyrke bruges når automatik er slået fra', () => {
  const manual = { brightness: { auto: false, minPercent: 15, maxPercent: 100, idlePercent: 25 } };
  assert.equal(brightnessFor(manual, 0), 100);
  assert.equal(brightnessFor(manual, 99999), 100);
});

test('hverdagsfilter gælder, så vinduet ikke kører i weekenden', () => {
  const kunHverdage = { schedule: { enabled: true, from: '06:30', to: '22:30', days: [1, 2, 3, 4, 5] } };
  // 2026-01-07 er en onsdag (day 3).
  assert.equal(decide(kunHverdage, { now: at(12, 0) }).on, true);
  // 2026-01-11 er en søndag (day 0).
  const sonning = new Date(2026, 0, 11, 12, 0, 0, 0);
  assert.equal(decide(kunHverdage, { now: sonning }).on, false);
});

test('nattevindue efter midnat tæller som forrige dags vindue', () => {
  const kunMandag = { schedule: { enabled: true, from: '22:00', to: '06:00', days: [1] } };
  const mandagAften = new Date(2026, 0, 5, 23, 0, 0, 0);
  const tirsdagNat = new Date(2026, 0, 6, 2, 0, 0, 0);
  const onsdagNat = new Date(2026, 0, 7, 2, 0, 0, 0);
  assert.equal(decide(kunMandag, { now: mandagAften }).on, true);
  assert.equal(decide(kunMandag, { now: tirsdagNat }).on, true);
  assert.equal(decide(kunMandag, { now: onsdagNat }).on, false);
});

test('ugyldige tidsfelter slår tidsplanen fra i stedet for at gå i uvedvarende loop', () => {
  const broken = { schedule: { enabled: true, from: 'xx', to: 'yy', days: [0, 1, 2, 3, 4, 5, 6] } };
  const result = decide(broken, { now: insideWindow });
  assert.equal(result.on, false);
});

test(' kan vaegten ikke maale lyset, dæmmer den for et mørkt rum', () => {
  // En sensor der ikke svarer maa ikke faa vaegget til at tro at rummet er
  // lyst. Fuld lysstyrke i et sovevaerrelse er et skarpt lys i hovedet.
  assert.equal(brightnessFor(DEFAULTS, null), DEFAULTS.brightness.minPercent);
  assert.equal(brightnessFor(DEFAULTS, undefined), DEFAULTS.brightness.minPercent);
});

test(' er auto-brightness slaaet fra, bruges den fulde lysstyrke', () => {
  const uden = { ...DEFAULTS, brightness: { ...DEFAULTS.brightness, auto: false } };
  assert.equal(brightnessFor(uden, 50), 100);
});
