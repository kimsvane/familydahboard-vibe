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
