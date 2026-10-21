// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { budgetLine, cancelSummary } from './autopilotText.ts';

test('the budget line shows the numbers it came from', () => {
  const g = { machine: { ram_bytes: 17.18e9, available_bytes: 3.0e9 }, budget_bytes: 2.7e9, budget_source: 'available' };
  assert.equal(
    budgetLine(g),
    'RAM 17.2 GB, available 3.0 GB -> budget 2.7 GB (90 % of the memory available now, smaller than 60 % of RAM)',
  );
});

test('the budget line without psutil (no available memory) or with a given budget', () => {
  assert.equal(budgetLine({ machine: { ram_bytes: 16e9, available_bytes: null }, budget_bytes: 9.6e9, budget_source: 'ram' }),
    'RAM 16.0 GB -> budget 9.6 GB (60 % of RAM)');
  assert.match(budgetLine({ machine: { ram_bytes: 16e9, available_bytes: 8e9 }, budget_bytes: 4.29e9, budget_source: 'given' }), /budget 4\.3 GB \(as given\)$/);
});

test('a cancelled run says what was cancelled and what was kept', () => {
  const s = cancelSummary([
    { id: 'check', title: 'Data check', status: 'done', files: ['a.json', 'b.json'] },
    { id: 'mosaic', title: 'Mosaic per time step', status: 'cancelled', files: ['m1.nc', 'm1.png'] },
  ]);
  assert.equal(s.kept, 4);
  assert.match(s.text, /^Cancelled during "Mosaic per time step"; 1 step finished before it, 4 files kept\. Run again resumes/);
});

test('no cancelled step, no cancel summary; a cancel before anything finished says so', () => {
  assert.equal(cancelSummary([{ id: 'a', title: 'A', status: 'done', files: [] }]), null);
  assert.match(cancelSummary([{ id: 'a', title: 'A', status: 'cancelled' }]).text, /no step had finished before it, 0 files kept/);
});
