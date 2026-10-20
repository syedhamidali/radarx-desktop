// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import { tidyServerError } from './errorText.ts';

test('an icechunk error keeps its sentence and loses the trace', () => {
  const raw =
    "list_arco_scans failed: Could not open ARCO store for KTBW:   x the repository doesn't exist\n" +
    '  | \n  | context:\n  |    0: icechunk::repository::open\n  |              at icechunk/src/repository.rs:343\n  | \n  `-> the repository doesn\'t exist';
  assert.equal(tidyServerError(raw), "list_arco_scans failed: Could not open ARCO store for KTBW: the repository doesn't exist");
});

test('plain messages pass through, long ones are cut, empty ones get a sentence', () => {
  assert.equal(tidyServerError('Not found: Unknown or closed file_id'), 'Not found: Unknown or closed file_id');
  assert.ok(tidyServerError('x'.repeat(1000)).length <= 280);
  assert.equal(tidyServerError(''), 'Unknown error from server');
  assert.equal(tidyServerError('Grid is 5 x 5 gates'), 'Grid is 5 x 5 gates');
  assert.equal(tidyServerError('Traceback (most recent call last):\n  File "a.py", line 1\nValueError: bad'), 'File "a.py", line 1 ValueError: bad');
});
