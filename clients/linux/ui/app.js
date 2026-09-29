'use strict';

const api = window.familyDashboard;
const page = document.body.dataset.page;
const status = document.querySelector('#status');
const input = document.querySelector('#server-url');

function setStatus(message, kind = '') {
  if (!status) {
    return;
  }
  status.textContent = message;
  status.className = `status ${kind}`.trim();
}

function setBusy(busy) {
  document.querySelectorAll('button').forEach((button) => {
    button.disabled = busy;
  });
}

function messageFrom(error) {
  return error?.message || 'Noget gik galt. Prøv igen.';
}

async function initialize() {
  if (!api) {
    setStatus('Kiosk-appen kunne ikke starte korrekt.', 'error');
    return;
  }
  const state = await api.getState();
  if (input && state.serverUrl) {
    input.value = state.serverUrl;
  }
  const offlineServer = document.querySelector('#server-url');
  if (offlineServer) {
    offlineServer.textContent = state.serverUrl || 'Serveradressen er ikke gemt endnu.';
  }
  if (state.configurationError) {
    setStatus(state.configurationError, 'error');
  }
}

async function testConnection() {
  if (!input?.value.trim()) {
    setStatus('Indtast serveradressen først.', 'error');
    return;
  }
  setBusy(true);
  setStatus('Tester forbindelsen …');
  try {
    const result = await api.testConnection({ serverUrl: input.value });
    setStatus(result.message, result.ok ? 'ok' : 'error');
  } catch (error) {
    setStatus(messageFrom(error), 'error');
  } finally {
    setBusy(false);
  }
}

async function saveConfig(closeAfterSave) {
  if (!input?.value.trim()) {
    setStatus('Indtast serveradressen først.', 'error');
    return;
  }
  setBusy(true);
  setStatus('Gemmer og forbinder …');
  try {
    await api.saveConfig({ serverUrl: input.value });
    setStatus('Gemt. Dashboardet indlæses nu.', 'ok');
    if (closeAfterSave) {
      setTimeout(() => api.closeControl(), 500);
    }
  } catch (error) {
    setStatus(messageFrom(error), 'error');
  } finally {
    setBusy(false);
  }
}

async function reconnect() {
  setBusy(true);
  setStatus('Genindlæser dashboardet …');
  try {
    await api.reconnect();
    if (page === 'control') {
      await api.closeControl();
    }
  } finally {
    setBusy(false);
  }
}

document.querySelector('#setup-form')?.addEventListener('submit', (event) => {
  event.preventDefault();
  saveConfig(false);
});
document.querySelector('#control-form')?.addEventListener('submit', (event) => {
  event.preventDefault();
  saveConfig(true);
});
document.querySelector('#test-button')?.addEventListener('click', testConnection);
document.querySelector('#reconnect-button')?.addEventListener('click', reconnect);
document.querySelector('#retry-button')?.addEventListener('click', reconnect);
document.querySelector('#settings-button')?.addEventListener('click', () => api.openControl());
document.querySelector('#close-button')?.addEventListener('click', () => api.closeControl());
document.querySelector('#handle-button')?.addEventListener('click', () => api.openControl());
document.querySelector('#quit-button')?.addEventListener('click', () => {
  if (window.confirm('Luk Family Dashboard på denne enhed?')) {
    api.quit();
  }
});

initialize().catch((error) => setStatus(messageFrom(error), 'error'));

// Vægindstillinger -----------------------------------------------------
// Surface Pro 4 har ingen præsens-sensor, så skærmen vækkes ved et tryk
// og ved bevægelse meldt fra Reolink-kameraet.

const wallForm = document.querySelector('#wall-form');
// Gemmes så formularen ikke mister værdier den ikke selv har et felt for.
let currentWallSettings = null;
const wallStatus = document.querySelector('#wall-status');
const wallResult = document.querySelector('#wall-result');
const wallCard = document.querySelector('#wall-card');
const sensorHelp = document.querySelector('#sensor-hjælp');

function setWallStatus(message, kind = '') {
  if (!wallStatus) return;
  wallStatus.textContent = message;
  wallStatus.className = `status ${kind}`.trim();
}

function setWallResult(message, kind = '') {
  if (!wallResult) return;
  wallResult.textContent = message;
  wallResult.className = `status ${kind}`.trim();
}

function selectedMode() {
  return wallForm?.querySelector('input[name="mode"]:checked')?.value ?? 'schedule';
}

function selectedDays() {
  return [...document.querySelectorAll('#schedule-days input:checked')]
    .map((input) => Number(input.value))
    .sort((a, b) => (a === 0 ? 7 : a) - (b === 0 ? 7 : b));
}

function currentMode() {
  const names = {
    schedule: 'Følger tidsplanen',
    presence: 'Kun når nogen er i nærheden',
    always: 'Altid tændt',
  };
  return names[selectedMode()] ?? 'Ukendt';
}

function readWallForm() {
  return {
    mode: selectedMode(),
    schedule: {
      enabled: document.querySelector('#schedule-enabled')?.checked ?? true,
      from: document.querySelector('#schedule-from')?.value || '06:30',
      to: document.querySelector('#schedule-to')?.value || '22:30',
      days: selectedDays(),
    },
    presence: {
      enabled: true,
      wakeOutsideSchedule: document.querySelector('#wake-outside')?.checked ?? true,
      sleepAfterIdleMinutes: Number(document.querySelector('#idle-minutes')?.value ?? 45),
      warmupGraceMinutes: 3,
      motionLeaseSeconds: currentWallSettings?.presence?.motionLeaseSeconds ?? 60,
    },
    display: {
      rotation: document.querySelector('#rotation-static')?.checked ? 'static' : 'auto',
      staticRotation: currentWallSettings?.display?.staticRotation ?? 'none',
    },
    brightness: {
      auto: document.querySelector('#brightness-auto')?.checked ?? true,
      darkLux: 5,
      brightLux: 400,
      minPercent: Number(document.querySelector('#brightness-min')?.value ?? 15),
      maxPercent: 100,
      idlePercent: 25,
    },
  };
}

/// Den valgte retning hører til den låste visning. Når skærmen følger
/// enheden, er der intet at vælge, og feltet grås derfor ud.
function syncRotation() {
  const fast = document.querySelector('#rotation-static')?.checked;
  const felt = document.querySelector('#rotation-felt');
  if (!felt) return;
  felt.toggleAttribute('disabled', !fast);
  felt.style.opacity = fast ? '1' : '0.5';
  for (const element of felt.querySelectorAll('select, input')) {
    element.disabled = !fast;
  }
}

function fillWallForm(settings) {
  currentWallSettings = settings ?? null;
  const rotation = settings?.display?.rotation ?? 'auto';
  const rotationRadio = document.querySelector(`input[name="rotation"][value="${rotation}"]`);
  if (rotationRadio) rotationRadio.checked = true;
  const value = document.querySelector('#rotation-static-value');
  if (value) value.value = settings?.display?.staticRotation ?? 'none';
  const radio = wallForm?.querySelector(`input[name="mode"][value="${settings?.mode ?? 'schedule'}"]`);
  if (radio) radio.checked = true;

  const s = (id, value) => {
    const element = document.querySelector(id);
    if (element) element.value = value;
  };
  const c = (id, value) => {
    const element = document.querySelector(id);
    if (element) element.checked = value === true;
  };

  c('#schedule-enabled', settings?.schedule?.enabled);
  s('#schedule-from', settings?.schedule?.from ?? '06:30');
  s('#schedule-to', settings?.schedule?.to ?? '22:30');
  c('#wake-outside', settings?.presence?.wakeOutsideSchedule);
  s('#idle-minutes', settings?.presence?.sleepAfterIdleMinutes ?? 45);
  c('#brightness-auto', settings?.brightness?.auto);
  s('#brightness-min', settings?.brightness?.minPercent ?? 15);

  const dage = Array.isArray(settings?.schedule?.days) ? settings.schedule.days : [0, 1, 2, 3, 4, 5, 6];
  document.querySelectorAll('#schedule-days input').forEach((input) => {
    input.checked = dage.includes(Number(input.value));
  });

  syncRelevance();
  syncRotation();
}

// Tidsplanen er meningsløs i præsenstilstand, så den grås ud i stedet for
// at stå og ligne at den gør noget.
function syncRelevance() {
  const mode = selectedMode();
  const scheduleField = document.querySelector('#schedule-felt');
  if (scheduleField) {
    scheduleField.dataset.irrelevant = mode === 'always' ? 'true' : 'false';
  }
  const wakeOutside = document.querySelector('#wake-outside');
  if (wakeOutside) {
    const label = wakeOutside.closest('label');
    if (label) label.dataset.irrelevant = mode === 'always' ? 'true' : 'false';
  }
  const brightnessField = document.querySelector('#brightness-felt');
  if (brightnessField) {
    brightnessField.dataset.irrelevant = document.querySelector('#brightness-auto')?.checked ? 'false' : 'true';
  }
}

function describeState(state) {
  if (!state) return 'Status er ikke tilgængelig.';
  const dele = [currentMode()];
  dele.push(state.on ? `tændt (${state.brightness}%)` : 'slukket');
  if (state.ambientLux !== null && state.ambientLux !== undefined) {
    dele.push(`${state.ambientLux} lux`);
  }
  if (state.reason === 'idle-timeout') dele.push('gik i dvale efter stilhed');
  if (state.reason === 'presence-outside-schedule') dele.push('vækket af bevægelse');
  return dele.join(' · ');
}

function describeSensors(sensors, writable) {
  const fundne = [];
  if (sensors?.light) fundne.push('lyssensor');
  if (sensors?.motion) fundne.push('bevægelsessensor');
  if (sensors?.gyro) fundne.push('gyroskop');
  const tekst = fundne.length ? fundne.join(', ') : 'ingen sensorer fundet';
  const rettigheder = writable ? 'skærmlysstyrken kan styres' : 'skærmlysstyrken kan ikke styres';
  if (sensorHelp) sensorHelp.textContent = `Fundet på maskinen: ${tekst}. ${rettigheder}.`;
}

async function loadWall() {
  if (!wallForm || !api) return;
  const data = await api.getWall();
  fillWallForm(data.settings);
  setWallStatus(describeState(data.state));
  describeSensors(data.sensors, data.display?.writable);
  api.subscribeWall((state) => setWallStatus(describeState(state)));
}

async function saveWall(event) {
  event?.preventDefault();
  setWallResult('Gemmer …');
  try {
    await api.saveWall(readWallForm());
    setWallResult('Gemt. Skærmen følger de nye regler med det samme.', 'ok');
  } catch (error) {
    setWallResult(messageFrom(error), 'error');
  }
}

if (wallForm) {
  wallCard?.removeAttribute('hidden');
  wallForm.addEventListener('submit', saveWall);
  wallForm.addEventListener('change', () => {
    syncRelevance();
    syncRotation();
  });
  document.querySelector('#screen-off-button')?.addEventListener('click', async () => {
    await api.setScreen({ on: false });
    setWallResult('Skærmen er slukket. Tryk på den for at vække den.', 'ok');
  });
  document.querySelector('#screen-on-button')?.addEventListener('click', async () => {
    await api.setScreen({ on: true });
    setWallResult('Skærmen er tændt.', 'ok');
  });
  loadWall().catch((error) => setWallResult(messageFrom(error), 'error'));
}
