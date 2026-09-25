'use strict';

function normalizeServerUrl(value) {
  if (typeof value !== 'string') {
    throw new Error('Indtast serveradressen.');
  }
  const trimmed = value.trim();
  if (!trimmed) {
    throw new Error('Indtast serveradressen.');
  }
  const hasSchemeDelimiter = trimmed.includes('://');
  const looksLikeHostPort = /^[^:/?#\s]+:\d+(?:[/?#]|$)/.test(trimmed);
  if (!hasSchemeDelimiter && !looksLikeHostPort && /^[a-z][a-z0-9+.-]*:/i.test(trimmed)) {
    throw new Error('Serveradressen skal begynde med http:// eller https://');
  }
  const candidate = hasSchemeDelimiter ? trimmed : `http://${trimmed}`;
  let parsed;
  try {
    parsed = new URL(candidate);
  } catch {
    throw new Error('Serveradressen er ikke gyldig.');
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error('Serveradressen skal begynde med http:// eller https://');
  }
  if (!parsed.hostname) {
    throw new Error('Serveradressen skal indeholde en vært.');
  }
  if (parsed.username || parsed.password) {
    throw new Error('Brugernavn og adgangskode er ikke tilladt i serveradressen.');
  }
  if (parsed.search || parsed.hash) {
    throw new Error('Serveradressen må ikke indeholde ? eller #.');
  }
  parsed.pathname = parsed.pathname.replace(/\/+$/, '') || '/';
  return parsed.toString();
}

function originOf(value) {
  const parsed = value instanceof URL ? value : new URL(value);
  return parsed.origin;
}

module.exports = { normalizeServerUrl, originOf };
