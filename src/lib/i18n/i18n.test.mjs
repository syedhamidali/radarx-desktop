// @ts-nocheck - plain node:test file, not part of the app bundle
import test from 'node:test';
import assert from 'node:assert/strict';
import { translate, registerLocale, compareCatalogues, availableLocales } from './index.ts';
import { en } from './messages/en.ts';

test('English is the source language and fills parameters', () => {
  assert.equal(translate('en', 'remoteOpen.cancel'), 'Cancel');
  assert.equal(translate('en', 'remoteOpen.reachedAt', { hostPort: 'localhost:8765' }), '(reached at localhost:8765)');
});

test('a missing translation falls back to English, then to the key, never blank', () => {
  registerLocale('xx', { 'remoteOpen.cancel': 'Abbrechen' });
  assert.equal(translate('xx', 'remoteOpen.cancel'), 'Abbrechen');
  assert.equal(translate('xx', 'remoteOpen.open'), 'Open'); // English
  assert.equal(translate('xx', 'no.such.key'), 'no.such.key');
});

test('a regional locale uses its language catalogue', () => {
  registerLocale('yy', { 'remoteOpen.cancel': 'Annuler' });
  assert.equal(translate('yy-CA', 'remoteOpen.cancel'), 'Annuler');
});

test('a parameter that is not given stays visible instead of breaking the text', () => {
  assert.equal(translate('en', 'remoteOpen.reachedAt', {}), '(reached at {hostPort})');
});

test('plural forms follow the language rules', () => {
  registerLocale('en', { ...en, 'files.opened_one': '{count} file opened', 'files.opened_other': '{count} files opened' });
  assert.equal(translate('en', 'files.opened', { count: 1 }), '1 file opened');
  assert.equal(translate('en', 'files.opened', { count: 3 }), '3 files opened');
  registerLocale('en', { ...en });
});

test('catalogue comparison reports missing and unknown keys', () => {
  const r = compareCatalogues({ a: 'x', b: 'y' }, { b: 'z', c: 'w' });
  assert.deepEqual(r, { missing: ['a'], unknown: ['c'] });
  assert.ok(availableLocales().includes('en'));
});
