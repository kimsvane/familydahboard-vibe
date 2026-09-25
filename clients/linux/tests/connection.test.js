'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { checkServer, healthUrl } = require('../src/connection');

test('builds the dashboard health url', () => {
  assert.equal(healthUrl('truenas:8080'), 'http://truenas:8080/api/health');
  assert.equal(healthUrl('https://example.com/dashboard/'), 'https://example.com/dashboard/api/health');
});

test('accepts a real family dashboard response', async () => {
  const requested = [];
  const result = await checkServer('truenas:8080', async (url, options) => {
    requested.push([url, options]);
    return {
      ok: true,
      status: 200,
      async json() {
        return { status: 'ok', service: 'family-dashboard', version: '0.1.0' };
      },
    };
  });
  assert.deepEqual(result, { ok: true, message: 'Forbindelsen virker.', version: '0.1.0' });
  assert.equal(requested[0][0], 'http://truenas:8080/api/health');
  assert.equal(requested[0][1].cache, 'no-store');
});

test('rejects a different service on the same port', async () => {
  const result = await checkServer('truenas:8080', async () => ({
    ok: true,
    status: 200,
    async json() {
      return { service: 'nginx' };
    },
  }));
  assert.equal(result.ok, false);
  assert.match(result.message, /ikke et Family Dashboard/);
});

test('turns network and timeout failures into touch friendly messages', async () => {
  const network = await checkServer('truenas:8080', async () => {
    throw new TypeError('fetch failed');
  });
  assert.match(network.message, /Kunne ikke forbinde/);
  const timeoutError = new Error('aborted');
  timeoutError.name = 'AbortError';
  const timeout = await checkServer('truenas:8080', async () => {
    throw timeoutError;
  });
  assert.match(timeout.message, /ikke i tide/);
});
