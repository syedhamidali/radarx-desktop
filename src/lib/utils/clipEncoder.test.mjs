// @ts-nocheck - plain node:test file, not part of the app bundle
// Run with Node >= 22.6:  npm run test:unit
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  pickCodecs, bitrateFor, avcLevelIdc, avcCodecString, vp9CodecString, frameTimestampUs, keyFrameInterval, fitPcm, pcmToPlanarF32, toBase64,
} from './clipEncoder.ts';

const all = { webcodecs: true, avc: true, vp9: true, aac: true, opus: true, avcHardware: false, notes: [] };

test('codec choice: H.264 + AAC in MP4 first, then VP9 + Opus in WebM, then nothing', () => {
  assert.deepEqual(pickCodecs(all), { container: 'mp4', ext: 'mp4', video: 'avc', audio: 'aac' });
  assert.deepEqual(pickCodecs({ ...all, avc: false }), { container: 'webm', ext: 'webm', video: 'vp9', audio: 'opus' });
  assert.equal(pickCodecs({ ...all, avc: false, vp9: false }), null);
  assert.equal(pickCodecs({ ...all, webcodecs: false }), null);
  // the picture without the sound codec: a silent film is still an MP4 (Opus in MP4 before silence)
  assert.deepEqual(pickCodecs({ ...all, aac: false }).audio, 'opus');
  assert.equal(pickCodecs({ ...all, aac: false, opus: false }).audio, null);
  assert.equal(pickCodecs({ ...all, avc: false, opus: false }).audio, null);
});

test('the bit rate follows the x264 CRF convention and stays within bounds', () => {
  const r18 = bitrateFor(1920, 1080, 30, 18);
  const r23 = bitrateFor(1920, 1080, 30, 23);
  const r24 = bitrateFor(1920, 1080, 30, 24);
  assert.ok(r18 > 12e6 && r18 < 14e6, `1080p30 at CRF 18 is about 13 Mbit/s, got ${r18}`);
  assert.ok(Math.abs(r23 - 1920 * 1080 * 30 * 0.12) < 1, 'CRF 23 is 0.12 bit per pixel per frame');
  assert.ok(Math.abs(bitrateFor(1920, 1080, 30, 17) / r23 - 2) < 0.01, 'six CRF steps double the rate');
  assert.ok(r24 < r23 && r18 > r23, 'lower CRF, higher rate');
  assert.equal(bitrateFor(64, 48, 1, 51), 250e3, 'floor');
  assert.equal(bitrateFor(7680, 4320, 120, 0), 80e6, 'ceiling');
  assert.ok(Number.isInteger(r18));
});

test('H.264 level and codec string from size and rate', () => {
  assert.equal(avcLevelIdc(1280, 720, 30), 31);
  assert.equal(avcLevelIdc(1920, 1080, 30), 40);
  assert.equal(avcLevelIdc(1920, 1080, 60), 42);
  assert.equal(avcLevelIdc(3840, 2160, 30), 51);
  assert.equal(avcLevelIdc(320, 180, 5), 30);
  assert.equal(avcCodecString(1920, 1080, 30), 'avc1.640028');
  assert.equal(avcCodecString(3840, 2160, 60), 'avc1.640034');
  assert.equal(vp9CodecString(1920, 1080, 30), 'vp09.00.40.08');
  assert.equal(vp9CodecString(320, 180, 5), 'vp09.00.21.08');
});

test('frame timestamps are exact multiples, never accumulated drift', () => {
  assert.equal(frameTimestampUs(0, 30), 0);
  assert.equal(frameTimestampUs(30, 30), 1_000_000);
  assert.equal(frameTimestampUs(3, 30), 100_000);
  assert.equal(frameTimestampUs(29, 30), 966_667);
  assert.equal(frameTimestampUs(900, 29.97), 30_030_030);
  assert.equal(keyFrameInterval(30), 60);
  assert.equal(keyFrameInterval(5), 10);
  assert.equal(keyFrameInterval(0.2), 1);
  assert.equal(keyFrameInterval(24, 1), 24);
});

test('the sound is cut or padded to exactly the length of the video', () => {
  const sr = 48000;
  const nFrames = 20, fps = 5; // 4 s = 192000 sample frames
  const long = new Int16Array(sr * 5 * 2).fill(7);
  const cut = fitPcm(long, sr, nFrames, fps);
  assert.equal(cut.length, 192000 * 2);
  assert.equal(cut[cut.length - 1], 7);
  const short = new Int16Array(sr * 3 * 2).fill(-3);
  const padded = fitPcm(short, sr, nFrames, fps);
  assert.equal(padded.length, 192000 * 2);
  assert.equal(padded[sr * 3 * 2 - 1], -3);
  assert.equal(padded[sr * 3 * 2], 0, 'silence after the end');
  assert.equal(padded[padded.length - 1], 0);
  const exact = new Int16Array(192000 * 2);
  assert.equal(fitPcm(exact, sr, nFrames, fps), exact, 'the same array when it already fits');
});

test('interleaved 16-bit stereo becomes planar float, left then right', () => {
  const pcm = new Int16Array([32767, -32768, 0, 16384]);
  const f = pcmToPlanarF32(pcm);
  assert.deepEqual(Array.from(f).map((v) => Math.round(v * 1e4) / 1e4), [1, 0, -1, 0.5]);
});

test('base64 of a byte range round-trips, in pieces larger than the 32 KB chunk', () => {
  const bytes = new Uint8Array(70000);
  for (let i = 0; i < bytes.length; i++) bytes[i] = (i * 7) & 255;
  const back = Buffer.from(toBase64(bytes), 'base64');
  assert.equal(back.length, bytes.length);
  assert.ok(back.equals(Buffer.from(bytes)));
  assert.equal(toBase64(new Uint8Array(0)), '');
});
