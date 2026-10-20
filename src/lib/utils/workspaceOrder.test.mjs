// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
//
// The right-hand panel order is saved with the workspace. The loader used to keep only the 13 panels
// it knew by name, so Rainfall (qpe) and Pipelines lost their place on every launch.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { PANEL_IDS } from '../utils/panelIds.ts';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..');

test('PANEL_IDS is exactly the panels the panel manager can show, in the same order', () => {
  const src = readFileSync(join(root, 'src/lib/components/PanelManager.svelte'), 'utf8');
  const block = src.slice(src.indexOf('const PANEL_MAP'), src.indexOf('};', src.indexOf('const PANEL_MAP')));
  const keys = [...block.matchAll(/^\s{4}(\w+): \{ lazy:/gm)].map((m) => m[1]);
  assert.deepEqual(keys, [...PANEL_IDS]);
});

test('a saved order keeps every panel, including qpe and pipeline', async () => {
  const saved = ['qpe', 'pipeline', 'export', 'layers', 'cloud', 'mosaic', 'wind', 'volume', 'timeseries', 'crosssection', 'sounding', 'qc', 'storm', 'modules', 'processing'];
  const store = new Map([
    ['xradar-workspaces', JSON.stringify({
      currentWorkspaceName: 'Default',
      autoSave: true,
      workspaces: { Default: { name: 'Default', rightPanelOrder: saved, rightPanelCollapsed: {} } },
    })],
  ]);
  globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)), removeItem: (k) => store.delete(k) };
  const { workspaceStore } = await import('../stores/workspace.ts');
  assert.deepEqual(workspaceStore.getCurrentConfig().rightPanelOrder, saved);
});
