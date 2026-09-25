'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { normalizeServerUrl, originOf } = require('../src/url');

test('adds http when the scheme is omitted', () => {
  assert.equal(normalizeServerUrl('truenas:8080'), 'http://truenas:8080/');
  assert.equal(normalizeServerUrl('192.168.1.20'), 'http://192.168.1.20/');
});

test('keeps an explicit https port and trims whitespace', () => {
  assert.equal(normalizeServerUrl('  https://dash.example.com:8443/  '), 'https://dash.example.com:8443/');
});

test('supports a dashboard under a subpath', () => {
  assert.equal(normalizeServerUrl('https://example.com/dashboard/'), 'https://example.com/dashboard');
});

test('rejects unsupported schemes, credentials and query strings', () => {
  assert.throws(() => normalizeServerUrl('ftp://example.com'), /http:\/\/ eller https:\/\//);
  assert.throws(() => normalizeServerUrl('http://user:pass@example.com'), /Brugernavn og adgangskode/);
  assert.throws(() => normalizeServerUrl('http://example.com?token=x'), /må ikke indeholde/);
  assert.throws(() => normalizeServerUrl('javascript:alert(1)'), /skal begynde/);
  assert.throws(() => normalizeServerUrl('   '), /Indtast serveradressen/);
});

test('returns the origin for navigation checks', () => {
  assert.equal(originOf('http://truenas:8080/'), 'http://truenas:8080');
  assert.equal(originOf('http://truenas:8080/dashboard'), 'http://truenas:8080');
});
