'use strict';

const DEFAULTS = Object.freeze({
  mode: 'schedule',
  schedule: { enabled: true, from: '06:30', to: '22:30', days: [0, 1, 2, 3, 4, 5, 6] },
  presence: {
    enabled: true,
    wakeOutsideSchedule: true,
    sleepAfterIdleMinutes: 45,
    warmupGraceMinutes: 3,
    // Sekunder skærmen holdes tændt efter en bevægelse fra kameraet.
    motionLeaseSeconds: 60,
  },
  display: {
    // 'auto' følger den fysiske hældning, 'static' låser retningen.
    rotation: 'auto',
    staticRotation: 'none',
  },
  brightness: {
    auto: true,
    darkLux: 5,
    brightLux: 400,
    minPercent: 15,
    maxPercent: 100,
    idlePercent: 25,
  },
});

/// "22:30" -> 1350 minutter siden midnat. Returnerer null ved ugyldigt input.
function parseTime(value) {
  if (typeof value !== 'string') return null;
  const match = /^(\d{1,2}):(\d{2})$/.exec(value.trim());
  if (!match) return null;
  const hours = Number(match[1]);
  const minutes = Number(match[2]);
  if (hours > 23 || minutes > 59) return null;
  return hours * 60 + minutes;
}

function minutesSinceMidnight(date) {
  return date.getHours() * 60 + date.getMinutes();
}

function isScheduledDay(settings, date) {
  const days = settings?.schedule?.days;
  if (!Array.isArray(days) || days.length === 0) return true;
  return days.includes(date.getDay());
}

/// Et tidsvindue der krydser midnat, f.eks. 22:00-06:00, er aktivt både
/// efter starttidspunktet og frem til sluttidspunktet næste morgen.
function inWindow(from, to, minutes) {
  if (from === null || to === null) return false;
  if (from === to) return true;
  if (from < to) return minutes >= from && minutes < to;
  return minutes >= from || minutes < to;
}

function isWithinSchedule(settings, date) {
  if (settings?.schedule?.enabled === false) return true;
  const from = parseTime(settings?.schedule?.from);
  const to = parseTime(settings?.schedule?.to);
  const minutes = minutesSinceMidnight(date);
  if (!inWindow(from, to, minutes)) return false;
  if (from === null || to === null || from <= to) {
    return isScheduledDay(settings, date);
  }
  // Efter midnat hører tiden til det vindue, der startede i går.
  if (minutes >= from) return isScheduledDay(settings, date);
  const yesterday = new Date(date);
  yesterday.setDate(yesterday.getDate() - 1);
  return isScheduledDay(settings, yesterday);
}

/// Lysstyrke som procent ud fra lux. Mørkt rum giver lavt lys, lyst rum får fuld.
function brightnessFor(settings, lux) {
  const config = settings?.brightness ?? DEFAULTS.brightness;
  // Uden auto-brightness er det fuld lysstyrke, brugeren har bedt om.
  if (!config.auto) return config.maxPercent;
  // Kan vi ikke maale lyset, skal vaegget vaere mørkt og ikke skinne i
  // hovedet paa et menneske. En sensor der ikke svarer er en god grund
  // til at vaere forsigtig, ikke til at antage at rummet er lyst.
  if (lux === null || lux === undefined) return config.minPercent;
  const dark = Number(config.darkLux);
  const bright = Number(config.brightLux);
  const min = Number(config.minPercent);
  const max = Number(config.maxPercent);
  if (bright <= dark) return max;
  const clamped = Math.max(dark, Math.min(bright, lux));
  const ratio = (clamped - dark) / (bright - dark);
  return Math.round(min + ratio * (max - min));
}

/// Hovedbeslutningen: skal skærmen være tændt, og hvor lys?
///
/// `state` er:
///   now                 Date
///   presenceDetected    bool - ser kameraet nogen lige nu
///   idleMinutes         tal - minutter siden sidste registrerede præsens
///   forcedOff           bool - f.eks. mens brugeren redigerer indstillinger
function decide(settings, state) {
  const config = { ...DEFAULTS, ...(settings ?? {}) };
  const now = state?.now instanceof Date ? state.now : new Date();
  const presence = state?.presenceDetected === true;
  const idleMinutes = Number.isFinite(state?.idleMinutes) ? state.idleMinutes : 0;
  const warmup = Number(config.presence.warmupGraceMinutes) || 0;
  const sleepAfter = Number(config.presence.sleepAfterIdleMinutes);
  const usePresence = config.presence.enabled === true;
  const lux = state?.ambientLux ?? null;

  if (state?.forcedOff) {
    return { on: false, brightness: 0, reason: 'forced-off' };
  }

  // Varm-up: lige efter at skærmen er vækket skal den have lidt ro, ellers
  // slukker den igen med det samme mens barnet stadig er ved at kravle op.
  const warming = warmup > 0 && idleMinutes < warmup;

  const idleTooLong = usePresence && Number.isFinite(sleepAfter) && sleepAfter > 0 && idleMinutes >= sleepAfter;

  if (config.mode === 'always') {
    return { on: true, brightness: brightnessFor(config, lux), reason: 'always' };
  }

  if (config.mode === 'presence') {
    if (presence) {
      return { on: true, brightness: brightnessFor(config, lux), reason: 'presence' };
    }
    return { on: false, brightness: 0, reason: 'no-presence' };
  }

  const scheduled = isWithinSchedule(config, now);

  if (!scheduled) {
    if (usePresence && presence && config.presence.wakeOutsideSchedule) {
      return { on: true, brightness: brightnessFor(config, lux), reason: 'presence-outside-schedule' };
    }
    return { on: false, brightness: 0, reason: 'outside-schedule' };
  }

  if (usePresence && !presence && !warming && idleTooLong) {
    return { on: false, brightness: 0, reason: 'idle-timeout' };
  }

  return { on: true, brightness: brightnessFor(config, lux), reason: warming ? 'warming' : 'schedule' };
}

module.exports = { DEFAULTS, brightnessFor, decide, inWindow, isWithinSchedule, minutesSinceMidnight, parseTime };
