'use strict';

const DEFAULT_INTERVAL_MS = 5000;

/// Holder øje med kameraernes AI-registreringer og melder fra når en ny
/// registrering dukker op. Dashboardens egen popup kan ikke vises på en
/// slukket skærm, så denne bro vækker skærmen først.
class CameraPresence {
  constructor(options = {}) {
    this.origin = String(options.origin ?? '').replace(/\/+$/, '');
    this.intervalMs = options.intervalMs ?? DEFAULT_INTERVAL_MS;
    this.leaseSeconds = options.leaseSeconds ?? 60;
    this.onMotion = options.onMotion ?? (() => {});
    this.onError = options.onError ?? (() => {});
    this.fetchImpl = options.fetchImpl ?? null;
    this.timer = null;
    this.seen = new Set();
    this.authorized = true;
    this.failures = 0;
  }

  get enabled() {
    return this.origin !== '' && this.intervalMs > 0;
  }

  start() {
    if (!this.enabled || this.timer) return;
    this.poll();
    this.timer = setInterval(() => this.poll(), this.intervalMs);
    this.timer.unref?.();
  }

  stop() {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    this.seen.clear();
  }

  /// Nøglen identificerer én registrering. Den indeholder kameraets id og
  /// starttidspunktet, så det samme kamera kan gen-melde senere.
  static keyFor(detection) {
    return `${detection?.id ?? '?'}:${detection?.since ?? '?'}`;
  }

  buildRequest(url) {
    if (this.fetchImpl) {
      return this.fetchImpl(url);
    }
    throw new Error('Ingen fetch tilgængelig');
  }

  async poll() {
    if (!this.enabled) return null;
    const url = `${this.origin}/api/cameras/activity`;
    try {
      const response = await this.buildRequest(url);
      if (response?.status === 401 || response?.status === 403) {
        // Uden gyldig session giver det ingen mening at fortsætte med at spørge.
        this.authorized = false;
        this.stop();
        this.onError(new Error('Kameraet kræver en ny login i dashboardet.'));
        return null;
      }
      if (!response?.ok) {
        throw new Error(`Dashboardet svarede HTTP ${response?.status ?? '?'}`);
      }
      const activity = await response.json();
      this.authorized = true;
      this.failures = 0;
      return this.handle(activity);
    } catch (error) {
      this.failures += 1;
      // Ét tabt svar skal ikke slå al bevægelsesregistrering fra, men
      // gentagne fejl skal give op i stedet for at køre i uendelig loop.
      if (this.failures >= 3) {
        this.stop();
        this.onError(error);
      }
      return null;
    }
  }

  handle(activity) {
    const active = Array.isArray(activity?.active) ? activity.active : [];
    const current = new Set(active.map(CameraPresence.keyFor));
    const fresh = active.filter((detection) => !this.seen.has(CameraPresence.keyFor(detection)));
    this.seen = current;

    if (fresh.length > 0) {
      const types = [...new Set(fresh.flatMap((detection) => (detection.types ?? []).map((item) => item.label || item.type)))];
      this.onMotion({ detections: fresh, types, leaseSeconds: this.leaseSeconds });
    }
    return { active, fresh };
  }
}

module.exports = { CameraPresence, DEFAULT_INTERVAL_MS };
