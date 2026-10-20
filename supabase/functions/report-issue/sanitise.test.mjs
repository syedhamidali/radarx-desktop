import test from 'node:test';
import assert from 'node:assert/strict';
import { hasNul, cpLength, truncate, cleanMultiline, cleanSingleLine, subjectText, escapeHtml } from './sanitise.ts';

test('NUL is detected (it is rejected, not repaired)', () => {
  assert.equal(hasNul('a\u0000b'), true);
  assert.equal(hasNul('ab'), false);
});

test('control characters go, newline and tab stay', () => {
  assert.equal(cleanMultiline('a\u0001b\u0007c\u001bd\u007fe\u0085f'), 'abcde\nf'); // U+0085 is a line break
  assert.equal(cleanMultiline('line1\nline2\tx'), 'line1\nline2\tx');
  assert.equal(cleanMultiline('a\u000Bb\u000Cc'), 'abc');
  assert.equal(cleanMultiline('a\u0080b\u009fc'), 'abc');
});

test('line endings are normalised', () => {
  assert.equal(cleanMultiline('a\r\nb\rc\u2028d\u2029e'), 'a\nb\nc\nd\ne');
});

test('bidirectional overrides, zero-width characters and BOM are removed', () => {
  assert.equal(cleanMultiline('ab\u202ecd\u2066ef\u200bgh\ufeffij'), 'abcdefghij');
  assert.equal(cleanMultiline('x\u200Dy\u2060z'), 'xyz');
});

test('lone surrogates are removed, real emoji stay', () => {
  assert.equal(cleanMultiline('a\ud800b\udc00c'), 'abc');
  assert.equal(cleanMultiline('ok \u{1F600} fine'), 'ok \u{1F600} fine');
});

test('Unicode is normalised to NFC', () => {
  assert.equal(cleanMultiline('e\u0301'), '\u00e9');
});

test('absurd whitespace runs are collapsed, normal indentation stays', () => {
  assert.equal(cleanMultiline('a' + ' '.repeat(50) + 'b'), 'a    b');
  assert.equal(cleanMultiline('a\t\t\t\t\t\t\t\t\t\tb'), 'a    b');
  assert.equal(cleanMultiline('a        b'), 'a        b'); // 8 spaces: kept
  assert.equal(cleanMultiline('a\n\n\n\n\n\n\nb'), 'a\n\n\nb');
  assert.equal(cleanMultiline('if x:\n    y = 1'), 'if x:\n    y = 1');
  assert.equal(cleanMultiline('a   \nb  '), 'a\nb');
});

test('the text is trimmed', () => {
  assert.equal(cleanMultiline('  \n hello \n\n'), 'hello');
  assert.equal(cleanMultiline(' \u00a0 \u3000'), '');
});

test('a single-line field has no line breaks at all', () => {
  assert.equal(cleanSingleLine('Title\r\nBcc: evil@example.org\nmore'), 'Title Bcc: evil@example.org more');
  assert.equal(cleanSingleLine('  a \t b  '), 'a b');
  assert.equal(/[\r\n]/.test(cleanSingleLine('x\u2028y\u0085z')), false);
});

test('lengths are counted in code points, like Postgres', () => {
  assert.equal(cpLength('a\u{1F600}b'), 3);
  assert.equal('a\u{1F600}b'.length, 4);
  assert.equal(truncate('a\u{1F600}b\u{1F600}', 3), 'a\u{1F600}b');
  assert.equal(truncate('abc', 10), 'abc');
});

test('the e-mail subject part is one line, shortened with an ellipsis, with no control characters', () => {
  const s = subjectText('Crash\r\nBcc: someone@example.org\u0000\u0007 now');
  assert.equal(/[\r\n\u0000-\u001f]/.test(s), false);
  assert.equal(s, 'Crash Bcc: someone@example.org now');
  const long = subjectText('x'.repeat(500), 100);
  assert.equal(cpLength(long), 100);
  assert.ok(long.endsWith('\u2026'));
  assert.equal(subjectText('short'), 'short');
});

test('HTML escaping covers the five characters and the backtick', () => {
  assert.equal(escapeHtml(`<img src=x onerror="alert('1')"> & \``), '&lt;img src=x onerror=&quot;alert(&#39;1&#39;)&quot;&gt; &amp; &#96;');
  assert.equal(escapeHtml('plain'), 'plain');
});
