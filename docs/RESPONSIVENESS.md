# Keeping the app fast and never "stuck"

Design rules, and where they live in the code.

## 1. Nothing waits forever (`src/lib/utils/requestScheduler.ts`, `websocket.ts`)
- Every long-running thing (sweep load, module run, mosaic, file open, export) is an **operation**
  (`beginOp`) with a state (`queued -> running -> slow -> failed|done|cancelled`), a "slow" mark
  after a few seconds and a timeout. Failures are retryable.
- **Sweep requests** go through a queue: at most 3 in flight, priorities `visible > prefetch >
  background`, identical requests share one wire request, a visible request always finds a free
  slot, and stale queued work is cancelled when the selection changes (`setViewContext`).
- A request that can never succeed (a variable missing on some sweeps of a NEXRAD split-cut
  volume) fails quietly as `unavailable` instead of hanging, and the viewer falls back to a
  variable the sweep has.
- A lost connection fails everything outstanding with "Connection lost"; Retry works afterwards.
- Errors attributed to a sweep request never reach unrelated handlers (they would otherwise fail a
  running module / export / open).

## 2. The user can always see and act (`OperationsTray.svelte`, `uiHealth.ts`)
- A status-bar chip lists running tasks with live elapsed time, Cancel / Retry / Dismiss; many
  sweep ops are grouped ("Loading sweeps 4/12"); background prefetch is hidden by default.
- A long-task monitor shows **UI busy** when the main thread stalls (> 200 ms).
- A progress bar that stops updating is flagged slow after 30 s and cleared after 60 s.

## 3. The 3D view has a frame budget (`qualityGovernor.ts`, `RadarViewer3D.svelte`)
- Quality levels minimal -> ultra control render scale, ray steps and volume texture size.
  Auto mode drops quality immediately when a frame is too costly and recovers slowly (hysteresis).
  Weak/software renderers start low. A Quality: Auto/High/Medium/Low control is in the toolbar.
- Frames are drawn in horizontal bands with a non-blocking fence, so one frame can never block the
  page; heavy CPU work (packing, upload) is time-sliced (~6 ms) and rebuilds are coalesced.
- Safety net: a band costing > 250 ms drops two levels and shows a hint; a lost context or shader
  failure shows "3D unavailable, switch to 2D" instead of looping.

## Known limits
- Cancelling an open / export / module stops *waiting*; it cannot abort the server job.
- Real-GPU behaviour of the 3D band logic is unmeasured (tested on software GL only).
- Cloud `open_nexrad_*` flows are not yet tracked as operations.
