'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

const { CameraPresence } = require('../src/cameras/presence');

function svar(aktiv, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => ({ active: aktiv, close_delay: 0, poll_seconds: 5 }),
  };
}

const person = (id = 1, siden = '2026-01-07T10:00:00+00:00') => ({
  id,
  name: 'Indgang',
  since: siden,
  types: [{ type: 'people', label: 'Person' }],
});

function build(aktiv, options = {}) {
  const events = [];
  const fejl = [];
  const presence = new CameraPresence({
    origin: 'https://dashboard.example',
    intervalMs: 10_000_000,
    fetchImpl: async () => svar(aktiv()),
    onMotion: (event) => events.push(event),
    onError: (error) => fejl.push(error),
    ...options,
  });
  return { presence, events, fejl };
}

test('broen er slået fra uden en serveradresse', () => {
  const presence = new CameraPresence({ origin: '', intervalMs: 5000 });
  assert.equal(presence.enabled, false);
  presence.start();
  assert.equal(presence.timer, null);
});

test('slafintervalet 0 slår broen fra', () => {
  assert.equal(new CameraPresence({ origin: 'https://x', intervalMs: 0 }).enabled, false);
});

test('trailinge skråstreger i serveradressen fjernes', () => {
  assert.equal(new CameraPresence({ origin: 'https://x///' }).origin, 'https://x');
});

test('første registrering meldes straks', async () => {
  let aktiv = [];
  const { presence, events } = build(() => aktiv);
  aktiv = [person()];
  await presence.poll();
  assert.equal(events.length, 1);
  assert.deepEqual(events[0].types, ['Person']);
  assert.equal(events[0].leaseSeconds, 60);
});

test('den samme registrering melder ikke igen ved næste afhentning', async () => {
  let aktiv = [person()];
  const { presence, events } = build(() => aktiv);
  await presence.poll();
  await presence.poll();
  await presence.poll();
  assert.equal(events.length, 1);
});

test('en ny registrering på samme kamera melder igen', async () => {
  let aktiv = [person(1, '2026-01-07T10:00:00+00:00')];
  const { presence, events } = build(() => aktiv);
  await presence.poll();
  assert.equal(events.length, 1);

  aktiv = [person(1, '2026-01-07T10:05:00+00:00')];
  await presence.poll();
  assert.equal(events.length, 2);
});

test('to kameraer på én afhentning giver én samlet hændelse', async () => {
  const aktiv = [person(1), { ...person(2), types: [{ type: 'vehicle', label: 'Køretøj' }] }];
  const { presence, events } = build(() => aktiv);
  await presence.poll();
  assert.equal(events.length, 1);
  assert.deepEqual(events[0].types.sort(), ['Køretøj', 'Person']);
});

test('ingen aktivitet giver ingen hændelse', async () => {
  const { presence, events } = build(() => []);
  const result = await presence.poll();
  assert.equal(events.length, 0);
  assert.deepEqual(result.active, []);
});

test('svaret uden active-felt tolkes som ingen aktivitet', async () => {
  const presence = new CameraPresence({
    origin: 'https://x',
    fetchImpl: async () => ({ ok: true, status: 200, json: async () => ({}) }),
    onMotion: () => {},
  });
  const result = await presence.poll();
  assert.deepEqual(result.active, []);
});

test('en ny hændelse efter en stille periode meldes igen', async () => {
  let aktiv = [];
  const { presence, events } = build(() => aktiv);
  aktiv = [person(1, 'A')];
  await presence.poll();
  aktiv = [];
  await presence.poll();
  aktiv = [person(1, 'B')];
  await presence.poll();
  assert.equal(events.length, 2);
});

test('en 401 slår broen fra i stedet for at spørge for evigt', async () => {
  const fejl = [];
  const { presence, events } = build(() => [], {
    fetchImpl: async () => svar([], 401),
    onError: (error) => fejl.push(error),
  });
  await presence.poll();
  assert.equal(events.length, 0);
  assert.equal(presence.authorized, false);
  assert.equal(fejl.length, 1);
  assert.match(fejl[0].message, /login/i);
});

test('gentagne serverfejl giver op efter tre forsøg', async () => {
  const fejl = [];
  const { presence } = build(() => [], {
    fetchImpl: async () => ({ ok: false, status: 500 }),
    onError: (error) => fejl.push(error),
  });
  await presence.poll();
  assert.equal(fejl.length, 0);
  await presence.poll();
  assert.equal(fejl.length, 0);
  await presence.poll();
  assert.equal(fejl.length, 1);
  assert.match(fejl[0].message, /HTTP 500/);
});

test('et enkelt fejlsvar slår ikke bevægelsesregistreringen fra', async () => {
  let fejlende = true;
  const { presence, events } = build(() => [person()], {
    fetchImpl: async () => {
      if (fejlende) {
        fejlende = false;
        throw new Error('netværksfejl');
      }
      return svar([person()]);
    },
    onError: () => {},
  });
  await presence.poll();
  const result = await presence.poll();
  assert.equal(result.fresh.length, 1, 'registreringen fanges efter genopretning');
  assert.equal(events.length, 1);
});

test('en vellykket afhentning nulstiller fejltælleren', async () => {
  let fejl = 0;
  const { presence } = build(() => [], {
    fetchImpl: async () => {
      fejl += 1;
      if (fejl <= 2) throw new Error('netværksfejl');
      return svar([]);
    },
    onError: () => {},
  });
  await presence.poll();
  await presence.poll();
  await presence.poll();
  assert.equal(presence.failures, 0);
});

test('nøglen adskiller registreringer på tidspunkt og kamera', () => {
  assert.equal(CameraPresence.keyFor(person(1, 'A')), '1:A');
  assert.notEqual(CameraPresence.keyFor(person(1, 'A')), CameraPresence.keyFor(person(1, 'B')));
  assert.notEqual(CameraPresence.keyFor(person(1, 'A')), CameraPresence.keyFor(person(2, 'A')));
});

test('en registrering uden kamera-id giver en brugbar nøgle', () => {
  assert.equal(CameraPresence.keyFor({}), '?:?');
});

test('stop rydder både timer og registrerede nøgler', () => {
  const { presence } = build(() => [person()]);
  presence.timer = setInterval(() => {}, 1000);
  presence.seen.add('1:A');
  presence.stop();
  assert.equal(presence.timer, null);
  assert.equal(presence.seen.size, 0);
});
