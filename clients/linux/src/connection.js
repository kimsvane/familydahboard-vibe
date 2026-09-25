'use strict';

const { normalizeServerUrl } = require('./url');

function healthUrl(serverUrl) {
  const base = new URL(normalizeServerUrl(serverUrl));
  base.pathname = `${base.pathname.replace(/\/$/, '')}/api/health`;
  base.search = '';
  base.hash = '';
  return base.toString();
}

async function checkServer(serverUrl, fetchImplementation = globalThis.fetch, timeoutMs = 6000) {
  const url = healthUrl(serverUrl);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchImplementation(url, {
      cache: 'no-store',
      redirect: 'follow',
      signal: controller.signal,
    });
    if (!response.ok) {
      return { ok: false, message: `Serveren svarede med HTTP ${response.status}.` };
    }
    const payload = await response.json();
    if (payload?.service !== 'family-dashboard') {
      return { ok: false, message: 'Adressen svarer, men er ikke et Family Dashboard.' };
    }
    return { ok: true, message: 'Forbindelsen virker.', version: payload.version || 'ukendt' };
  } catch (error) {
    if (error?.name === 'AbortError') {
      return { ok: false, message: 'Serveren svarede ikke i tide.' };
    }
    return { ok: false, message: 'Kunne ikke forbinde til serveren. Kontrollér adresse og netværk.' };
  } finally {
    clearTimeout(timeout);
  }
}

module.exports = { checkServer, healthUrl };
