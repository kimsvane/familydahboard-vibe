'use strict';

const { DEFAULTS, decide } = require('./policy');
const { readAmbientLight } = require('../sensors/iio');
const screen = require('./screen');

const POLL_MS = 5000;

/// Holder øje med sensorerne og beslutter, hvornår skærmen skal være tændt.
///
/// Præsens kommer fra to kilder, fordi Surface Pro 4 ikke har en
/// præsens-sensor: et tryk på skærmen og en ekstern bevægelsesmelding.
/// Touch tæller med det samme, så skærmen reagerer øjeblikkeligt.
class WallSupervisor {
  constructor(options = {}) {
    this.settings = options.settings ?? DEFAULTS;
    this.pollMs = options.pollMs ?? POLL_MS;
    this.timer = null;
    // Klokken skal være klar, før tilstanden bruges til at tidsstemple starten.
    this.now = options.now ?? (() => Date.now());
    this.clock = options.clock ?? (() => new Date(this.now()));
    this.state = {
      on: true,
      brightness: 100,
      reason: 'start',
      lastChangeAt: this.now(),
      // Tilstedeværelse er en tidsbegrænset tilladelse, ikke en kontakt der
      // bliver stående. Ellers ville ét tryk holde skærmen tændt for evigt.
      presenceUntil: null,
      // Uden en starttidspunkt ville den første dvale-periode aldrig kunne
      // løbe ud, fordi "tid siden sidste præsens" så ville være 0 lige meget.
      lastPresenceAt: this.now(),
    };
    this.onChange = options.onChange ?? (() => {});
    this.onSleep = options.onSleep ?? (() => {});
    this.onWake = options.onWake ?? (() => {});
    this.forcedOff = false;
    this.presenceEnabled = options.presenceEnabled ?? false;
    this.readLight = options.readLight ?? readAmbientLight;
    this.screenControl = options.screenControl ?? screen;
  }

  start() {
    if (this.timer) return;
    this.tick();
    this.timer = setInterval(() => this.tick(), this.pollMs);
  }

  stop() {
    if (!this.timer) return;
    clearInterval(this.timer);
    this.timer = null;
  }

  /// Et tryk tæller som præsens i en kort periode og fornyer samtidig
  /// dvale-fristen. Er der ingen ny aktivitet bagefter, falder tilstedeværelsen
  /// væk, og skærmen går i dvale når den samlede ventetid er brugt op.
  registerTouch() {
    const now = this.now();
    this.state.lastPresenceAt = now;
    this.state.presenceUntil = now + this.presenceLeaseMs();
    this.tick();
  }

  presenceLeaseMs() {
    const minutes = Number(this.settings?.presence?.warmupGraceMinutes);
    return (Number.isFinite(minutes) && minutes > 0 ? minutes : 3) * 60_000;
  }

  presenceDetected() {
    return this.state.presenceUntil !== null && this.now() < this.state.presenceUntil;
  }

  /// Bevægelse meldt af Reolink-kameraet, som står et andet sted i huset.
  registerExternalPresence(seconds = 0) {
    const now = this.now();
    this.state.lastPresenceAt = now;
    // Bevægelsen holder præsens "levende" i det angivne antal sekunder, så et
    // kort kamera-blink ved døren ikke tæller som fristende udløbet.
    this.state.presenceUntil = now + Math.max(0, seconds) * 1000;
    this.tick();
  }

  setForcedOff(value) {
    this.forcedOff = value === true;
    this.tick();
  }

  updateSettings(settings) {
    this.settings = settings ?? DEFAULTS;
    this.tick();
  }

  idleMinutes() {
    // Naar der endnu ikke er set nogen, tæller tiden siden appen startede,
    // så skærmen stadig kan gå i dvale efter en stille periode.
    const reference = this.state.lastPresenceAt ?? this.state.lastChangeAt;
    if (reference === null || reference === undefined) return Number.POSITIVE_INFINITY;
    return (this.now() - reference) / 60000;
  }

  tick() {
    const light = this.readLight();
    const decision = decide(this.settings, {
      now: this.clock(),
      presenceDetected: this.presenceDetected(),
      idleMinutes: this.idleMinutes(),
      ambientLux: light?.lux ?? null,
      forcedOff: this.forcedOff,
    });

    const wasOn = this.state.on;
    // Første gennemløb skal altid anvende beslutningen, fordi vi endnu ikke
    // ved hvordan skærmen faktisk står, når appen starter.
    const firstRun = this.state.applied !== true;
    const brightnessChanged = decision.on && decision.brightness !== this.state.brightness;

    this.state = { ...this.state, ...decision, lastChangeAt: this.now(), applied: true };

    if (decision.on) {
      if (!wasOn) {
        // Skærmen vågner med den lysstyrke der passer til lyset i rummet,
        // så den ikke blinker hvid i et mørkt køkken.
        this.screenControl.turnOn(decision.brightness, () => {});
        this.onWake(decision);
      } else if (firstRun || brightnessChanged) {
        this.screenControl.setBrightnessPercent(decision.brightness);
      }
    } else if (wasOn || firstRun) {
      this.screenControl.turnOff(() => {});
      this.onSleep(decision);
    }

    this.onChange(this.snapshot(light));
    return decision;
  }

  snapshot(light = null) {
    return {
      on: this.state.on,
      brightness: this.state.brightness,
      reason: this.state.reason,
      present: this.presenceDetected(),
      idleMinutes: Number.isFinite(this.idleMinutes()) ? Math.round(this.idleMinutes()) : null,
      ambientLux: light?.lux ?? null,
      lightSensor: this.readLight !== readAmbientLight ? null : light?.lux ?? null,
      sensors: {
        light: light !== null,
        brightnessWritable: this.screenControl.canWriteBrightness(),
      },
    };
  }
}

module.exports = { POLL_MS, WallSupervisor };
