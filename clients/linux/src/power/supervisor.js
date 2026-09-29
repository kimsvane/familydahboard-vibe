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
    this.state = {
      on: true,
      brightness: 100,
      reason: 'start',
      lastChangeAt: Date.now(),
      presenceSince: null,
      lastPresenceAt: null,
    };
    this.onChange = options.onChange ?? (() => {});
    this.onSleep = options.onSleep ?? (() => {});
    this.onWake = options.onWake ?? (() => {});
    this.forcedOff = false;
    this.presenceEnabled = options.presenceEnabled ?? false;
    this.readLight = options.readLight ?? readAmbientLight;
    this.screenControl = options.screenControl ?? screen;
    this.clock = options.clock ?? (() => new Date());
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

  /// Et tryk på skærmen tæller som præsens og fornyer tidsfristen.
  registerTouch() {
    this.state.lastPresenceAt = Date.now();
    if (this.state.presenceSince === null) {
      this.state.presenceSince = Date.now();
    }
    this.tick();
  }

  /// Bevægelse meldt af Reolink-kameraet, som står et andet sted i huset.
  registerExternalPresence(seconds = 0) {
    const now = Date.now();
    this.state.lastPresenceAt = now;
    // Bevægelsen holder præsens "levende" i det angivne antal sekunder, så et
    // kort kamera-blink ved døren ikke tæller som fristende udløbet.
    this.state.presenceSince = now - Math.max(0, seconds) * 1000;
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
    if (this.state.lastPresenceAt === null) return Number.POSITIVE_INFINITY;
    return (Date.now() - this.state.lastPresenceAt) / 60000;
  }

  tick() {
    const light = this.readLight();
    const decision = decide(this.settings, {
      now: this.clock(),
      presenceDetected: this.state.presenceSince !== null,
      idleMinutes: this.idleMinutes(),
      ambientLux: light?.lux ?? null,
      forcedOff: this.forcedOff,
    });

    const wasOn = this.state.on;
    // Første gennemløb skal altid anvende beslutningen, fordi vi endnu ikke
    // ved hvordan skærmen faktisk står, når appen starter.
    const firstRun = this.state.applied !== true;
    const brightnessChanged = decision.on && decision.brightness !== this.state.brightness;

    this.state = { ...this.state, ...decision, lastChangeAt: Date.now(), applied: true };

    if (decision.on) {
      if (!wasOn) {
        this.screenControl.turnOn(() => {});
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
      present: this.state.presenceSince !== null,
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
