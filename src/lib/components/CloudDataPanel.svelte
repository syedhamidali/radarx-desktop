<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { wsManager } from '../utils/websocket';
  import { connectionStatus, processingProgress } from '../stores/radarData';
  import { beginOp, cancelOp, type OpHandle } from '../utils/requestScheduler';
  import { addToast } from '../stores/toasts';
  import { activeFile } from '../stores/fileManager';
  import { webUi } from '../stores/webInfo';
  import CollapsiblePanel from './CollapsiblePanel.svelte';
  import {
    findNeighbours,
    nearestScan,
    utcTargetMs,
    candidateDays,
    stationFromName,
    formatRelative,
    parseScanTime,
  } from '../utils/radarNetwork';
  import { takePendingCloudRequest } from '../utils/cloudRequest';

  type Station = {
    id: string;
    arco: boolean;
    latitude?: number;
    longitude?: number;
    elevation_m?: number;
  };
  type L2File = { name: string; path: string; size: number };
  type ArcoScan = { key: string; path: string };

  const isConnected = $derived($connectionStatus === 'connected');

  // ── State ────────────────────────────────────────────────────────────────
  let mode = $state<'realtime' | 'archive' | 'arco' | 'network'>('realtime');
  let stations = $state<Station[]>([]);
  let selectedStation = $state('KLOT');
  let loadingStations = $state(false);

  // Archive (Level II)
  let archiveDate = $state(todayStr());
  let l2Files = $state<L2File[]>([]);
  let loadingFiles = $state(false);

  // ARCO
  let arcoScans = $state<ArcoScan[]>([]);
  let loadingArco = $state(false);

  // Opening state: one cloud open at a time, tracked as an operation in the tray.
  // `openKey` identifies what is in flight ("realtime:KLOT", "l2:<path>", "arco:<station>:<key>")
  // so only that control shows "Opening…".
  let isOpening = $state(false);
  let openKey = $state<string | null>(null);
  let cloudOp: OpHandle | null = null;
  let cloudOpUnsub: (() => void) | null = null;
  const CLOUD_OPEN_TIMEOUT_MS = 120_000;
  /** Server errors that belong to the cloud open flows ("open_arco_scan failed: ...", "Invalid input for open_nexrad_l2: ..."). */
  const OPEN_ERROR_RE = /(?:^|\b(?:in|for)\s+)(?:open_nexrad_realtime|open_nexrad_l2|open_arco_scan)\b/;

  // Radar network (several nearby sites at about the same time → mosaic)
  type NetRow = { id: string; state: 'pending' | 'running' | 'ok' | 'failed'; text: string };
  /** Longest tolerated gap between the requested time and a site's nearest volume. */
  const NET_MAX_GAP_MIN = 30;
  const LIST_TIMEOUT_MS = 60_000;
  const OPEN_TIMEOUT_MS = CLOUD_OPEN_TIMEOUT_MS;
  let netRadius = $state(200);
  let netMax = $state(4);
  let netTimeMode = $state<'latest' | 'time'>('latest');
  let netDate = $state(todayStr());
  let netTime = $state(nowTimeStr());
  let netExcluded = $state<string[]>([]);
  let netBusy = $state(false);
  let netCancel = false;
  let netRows = $state<NetRow[]>([]);

  const centre = $derived(selectedStation.trim().toUpperCase());
  const centreStation = $derived(stations.find((s) => s.id === centre));
  const centreHasCoords = $derived(
    Number.isFinite(centreStation?.latitude) && Number.isFinite(centreStation?.longitude),
  );
  // The centre always counts towards the max radar count.
  const neighbours = $derived(findNeighbours(centre, stations, netRadius, netMax - 1));
  const netSelected = $derived([
    centre,
    ...neighbours.filter((n) => !netExcluded.includes(n.id)).map((n) => n.id),
  ]);
  const netTargetMs = $derived(netTimeMode === 'time' ? utcTargetMs(netDate, netTime) : 0);
  const netTimeValid = $derived(netTimeMode === 'latest' || Number.isFinite(netTargetMs));
  /** Station of the active file (from its name), when it is a known NEXRAD site. */
  const activeSite = $derived.by(() => {
    const f = $activeFile;
    if (!f) return null;
    const id = stationFromName(f.filename) ?? stationFromName(f.path);
    return id && stations.some((s) => s.id === id) ? id : null;
  });

  // ── Subscriptions ────────────────────────────────────────────────────────
  let unsubStations: (() => void) | null = null;
  let unsubFiles: (() => void) | null = null;
  let unsubArco: (() => void) | null = null;
  let unsubOpened: (() => void) | null = null;
  let unsubError: (() => void) | null = null;
  let unsubDrop: (() => void) | null = null;
  let unsubWebUi: (() => void) | null = null;

  /** Welcome screen (web): switch to the real-time tab and fetch the latest scan. */
  function onOpenLatestRequest() {
    mode = 'realtime';
    openRealtime();
  }

  /** Radar sites card: list this station's archive, or open the exact file the card showed. */
  function applySiteRequest() {
    const r = takePendingCloudRequest();
    if (!r) return;
    selectedStation = r.station;
    if (r.action === 'open') {
      mode = 'realtime';
      openL2File({ name: r.name, path: r.path, size: r.size ?? 0 });
    } else {
      mode = 'archive';
      archiveDate = todayStr();
      fetchL2Files();
    }
  }

  onMount(() => {
    window.addEventListener('xradar:cloud-site-apply', applySiteRequest);
    applySiteRequest(); // a request that arrived while this panel was still loading
    // Hosted web app: the first thing a visitor sees is the latest scan of a busy radar.
    unsubWebUi = webUi.subscribe((on) => {
      if (on && selectedStation === 'KLOT') selectedStation = 'KTLX';
    });
    window.addEventListener('xradar:cloud-open-latest', onOpenLatestRequest);

    unsubStations = wsManager.onMessage('nexrad_stations', (msg: any) => {
      stations = msg.stations ?? [];
      loadingStations = false;
    });
    unsubFiles = wsManager.onMessage('nexrad_l2_files', (msg: any) => {
      // Listings requested by the radar-network run must not clobber the Archive list.
      if (netBusy) return;
      l2Files = msg.files ?? [];
      loadingFiles = false;
    });
    unsubArco = wsManager.onMessage('arco_scans', (msg: any) => {
      arcoScans = msg.scans ?? [];
      loadingArco = false;
    });
    unsubOpened = wsManager.onMessage('file_opened', () => {
      endCloudOp();
    });
    unsubError = wsManager.onMessage('error', (msg: any) => {
      const text = String(msg?.message ?? 'Open failed');
      // Only an error from one of the open flows ends the pending open; an unrelated error
      // (a sweep, a module) must not re-enable the button while the download is still running.
      if (OPEN_ERROR_RE.test(text)) endCloudOp(text.replace(/^[a-z_0-9]+ failed:\s*/i, ''));
      loadingFiles = false;
      loadingArco = false;
    });

    // A dropped connection ends a pending open now instead of after the 120 s timeout.
    unsubDrop = connectionStatus.subscribe((s) => {
      if (s !== 'connected' && cloudOp) endCloudOp('Connection to the sidecar was lost');
    });

    // Auto-fetch station list once connected
    const unsubConn = connectionStatus.subscribe((s) => {
      if (s === 'connected' && stations.length === 0) {
        loadingStations = true;
        wsManager.send({ type: 'list_nexrad_stations' });
        // The store calls back at once with its current value: unsubscribe once `unsubConn` exists.
        queueMicrotask(() => unsubConn());
      }
    });
  });

  onDestroy(() => {
    window.removeEventListener('xradar:cloud-open-latest', onOpenLatestRequest);
    window.removeEventListener('xradar:cloud-site-apply', applySiteRequest);
    unsubWebUi?.();
    if (cloudOp) cancelOp(cloudOp.id);
    unsubStations?.();
    unsubFiles?.();
    unsubArco?.();
    unsubOpened?.();
    unsubError?.();
    unsubDrop?.();
  });

  // ── Helpers ──────────────────────────────────────────────────────────────
  function todayStr(): string {
    return new Date().toISOString().slice(0, 10);
  }

  function nowTimeStr(): string {
    return new Date().toISOString().slice(11, 16);
  }

  function formatBytes(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(0)} KB`;
    return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  }

  // ── Actions ──────────────────────────────────────────────────────────────
  function fetchStations() {
    if (!isConnected) return;
    loadingStations = true;
    stations = [];
    wsManager.send({ type: 'list_nexrad_stations' });
  }

  function fetchL2Files() {
    if (!isConnected || !selectedStation || !archiveDate) return;
    loadingFiles = true;
    l2Files = [];
    wsManager.send({ type: 'list_nexrad_l2_files', station: selectedStation, date: archiveDate });
  }

  function fetchArcoScans() {
    if (!isConnected || !selectedStation) return;
    loadingArco = true;
    arcoScans = [];
    wsManager.send({ type: 'list_arco_scans', station: selectedStation, limit: 50 });
  }

  /** Stop tracking the in-flight cloud open (success, or failure with a reason). */
  function endCloudOp(error?: string) {
    const op = cloudOp;
    cloudOp = null;
    cloudOpUnsub?.();
    cloudOpUnsub = null;
    isOpening = false;
    openKey = null;
    if (!op) return;
    if (error) op.fail(error);
    else op.done();
  }

  /**
   * Show a cloud open in the operations tray: progress text from the global progress messages,
   * Cancel = stop waiting (the server cannot abort the download), Retry re-sends, and a timeout
   * fails the op so the button never stays stuck on "Opening…".
   */
  function beginCloudOp(key: string, label: string, send: () => void) {
    if (cloudOp) cancelOp(cloudOp.id);
    isOpening = true;
    openKey = key;
    const run = () => {
      if (!isConnected || isOpening) return;
      beginCloudOp(key, label, send);
    };
    const op: OpHandle = beginOp({
      kind: 'open',
      label,
      slowMs: 8_000,
      timeoutMs: CLOUD_OPEN_TIMEOUT_MS,
      onCancel: () => {
        if (cloudOp === op) endCloudOp();
      },
      onTimeout: () => {
        if (cloudOp === op) {
          cloudOp = null;
          cloudOpUnsub?.();
          cloudOpUnsub = null;
          isOpening = false;
          openKey = null;
        }
      },
      onRetry: run,
    });
    cloudOp = op;
    cloudOpUnsub = processingProgress.subscribe((p) => {
      if (p && p.percent < 100 && cloudOp === op) {
        op.update(`${p.message || 'Working'} (${Math.round(p.percent)}%)`);
      }
    });
    addToast('info', `${label}...`);
    send();
  }

  function openRealtime() {
    if (!isConnected || !selectedStation || isOpening || netBusy) return;
    const station = selectedStation.trim().toUpperCase();
    beginCloudOp(`realtime:${station}`, `Opening latest ${station} scan`, () =>
      wsManager.send({ type: 'open_nexrad_realtime', station }),
    );
  }

  function openL2File(file: L2File) {
    if (!isConnected || isOpening || netBusy) return;
    beginCloudOp(`l2:${file.path}`, `Opening ${file.name.slice(-19)}`, () =>
      wsManager.send({ type: 'open_nexrad_l2', path: file.path }),
    );
  }

  function openArcoScan(scan: ArcoScan) {
    if (!isConnected || isOpening || netBusy) return;
    const station = selectedStation;
    beginCloudOp(`arco:${station}:${scan.key}`, `Opening ARCO scan ${scan.key}`, () =>
      wsManager.send({ type: 'open_arco_scan', station, scan_key: scan.key }),
    );
  }

  // ── Radar network ────────────────────────────────────────────────────────
  /**
   * Send one request and resolve with the first matching `okType` reply, or reject on a
   * matching server error, a dropped connection, or timeout. Requests of a network run are
   * strictly sequential, so replies need no ids; unrelated errors are ignored by prefix.
   */
  function request(
    send: () => void,
    okType: string,
    match: (msg: any) => boolean,
    errorPrefix: string,
    timeoutMs: number,
    signal?: AbortSignal,
  ): Promise<any> {
    return new Promise((resolve, reject) => {
      const unsubs: Array<() => void> = [];
      let done = false;
      const finish = (fn: () => void) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        unsubs.forEach((u) => u());
        fn();
      };
      const timer = setTimeout(() => finish(() => reject(new Error('timed out'))), timeoutMs);
      unsubs.push(wsManager.onMessage(okType, (m: any) => { if (match(m)) finish(() => resolve(m)); }));
      unsubs.push(
        wsManager.onMessage('error', (m: any) => {
          const text = String(m?.message ?? 'unknown error');
          if (text.startsWith(errorPrefix)) {
            finish(() => reject(new Error(text.replace(/^[a-z_0-9]+ failed:\s*/i, ''))));
          }
        }),
      );
      // connectionStatus is a store: the first callback is the current value.
      let first = true;
      const unsubConn = connectionStatus.subscribe((st) => {
        if (first) { first = false; return; }
        if (st !== 'connected') finish(() => reject(new Error('connection lost')));
      });
      unsubs.push(unsubConn);
      if (signal) {
        if (signal.aborted) return finish(() => reject(new Error('cancelled')));
        const onAbort = () => finish(() => reject(new Error('cancelled')));
        signal.addEventListener('abort', onAbort, { once: true });
        unsubs.push(() => signal.removeEventListener('abort', onAbort));
      }
      send();
    });
  }

  function setRow(i: number, patch: Partial<NetRow>) {
    netRows = netRows.map((r, k) => (k === i ? { ...r, ...patch } : r));
  }

  function toggleExcluded(id: string) {
    netExcluded = netExcluded.includes(id) ? netExcluded.filter((x) => x !== id) : [...netExcluded, id];
  }

  async function listDayFiles(station: string, date: string): Promise<L2File[]> {
    const m = await request(
      () => wsManager.send({ type: 'list_nexrad_l2_files', station, date }),
      'nexrad_l2_files',
      (r) => r.station === station && r.date === date,
      'list_nexrad_l2_files',
      LIST_TIMEOUT_MS,
    );
    return m.files ?? [];
  }

  /**
   * Run one network-site open as an operation in the tray (progress text, Cancel = stop waiting).
   * The request's own timeout ends it; the op mirrors the outcome.
   */
  async function trackedOpen<T>(label: string, run: (signal: AbortSignal) => Promise<T>): Promise<T> {
    const op = beginOp({ kind: 'open', label, slowMs: 8_000, timeoutMs: 0 });
    const unsub = processingProgress.subscribe((p) => {
      if (p && p.percent < 100) op.update(`${p.message || 'Working'} (${Math.round(p.percent)}%)`);
    });
    try {
      const r = await run(op.signal);
      op.done();
      return r;
    } catch (e) {
      // A cancelled op is already finished by cancelOp; failing it again would overwrite that.
      if (!op.signal.aborted) op.fail((e as Error).message ?? String(e));
      throw e;
    } finally {
      unsub();
    }
  }

  /** Open one site's volume nearest the target (or its latest); resolves with the new file id. */
  async function openSite(i: number, station: string, total: number): Promise<string> {
    const tag = `${station} ${i + 1}/${total}`;
    let reply: any;
    if (netTimeMode === 'latest') {
      setRow(i, { state: 'running', text: `${tag} — fetching latest scan…` });
      reply = await trackedOpen(`Opening latest ${tag}`, (signal) =>
        request(
          () => wsManager.send({ type: 'open_nexrad_realtime', station }),
          'file_opened',
          () => true,
          'open_nexrad_realtime',
          OPEN_TIMEOUT_MS,
          signal,
        ),
      );
    } else {
      setRow(i, { state: 'running', text: `${tag} — listing scans…` });
      const files: L2File[] = [];
      let listError: Error | null = null;
      for (const day of candidateDays(netTargetMs)) {
        try {
          files.push(...(await listDayFiles(station, day)));
        } catch (e) {
          listError = e as Error;
        }
      }
      const pick = nearestScan(files, netTargetMs);
      if (!pick) throw listError ?? new Error('no scans on that day');
      const gapMin = Math.abs((parseScanTime(pick.name) ?? netTargetMs) - netTargetMs) / 60000;
      if (gapMin > NET_MAX_GAP_MIN) {
        throw new Error(`nearest scan is ${Math.round(gapMin)} min from the target`);
      }
      setRow(i, { state: 'running', text: `${tag} — opening ${pick.name.slice(-19)} (Δ${Math.round(gapMin)} min)…` });
      reply = await trackedOpen(`Opening ${tag} ${pick.name.slice(-19)}`, (signal) =>
        request(
          () => wsManager.send({ type: 'open_nexrad_l2', path: pick.path }),
          'file_opened',
          () => true,
          'open_nexrad_l2',
          OPEN_TIMEOUT_MS,
          signal,
        ),
      );
    }
    if (!reply?.file_id) throw new Error('server returned no file id');
    return reply.file_id as string;
  }

  async function openNetwork() {
    if (!isConnected || netBusy || !netTimeValid || netSelected.length < 2) return;
    const sites = [...netSelected];
    netBusy = true;
    netCancel = false;
    netRows = sites.map((id, i) => ({ id, state: 'pending', text: `${id} ${i + 1}/${sites.length} — waiting` }));
    const fileIds: string[] = [];
    const failed: string[] = [];
    try {
      for (let i = 0; i < sites.length; i++) {
        if (netCancel) {
          for (let k = i; k < sites.length; k++) setRow(k, { state: 'failed', text: `${sites[k]} ${k + 1}/${sites.length} — cancelled` });
          break;
        }
        const tag = `${sites[i]} ${i + 1}/${sites.length}`;
        try {
          fileIds.push(await openSite(i, sites[i], sites.length));
          setRow(i, { state: 'ok', text: `${tag} — opened` });
        } catch (e) {
          const reason = (e as Error).message ?? String(e);
          failed.push(`${sites[i]} (${reason})`);
          setRow(i, { state: 'failed', text: `${tag} — failed: ${reason}` });
        }
      }
    } finally {
      netBusy = false;
    }
    if (failed.length > 0) addToast('warning', `Radar network: ${failed.length} failed — ${failed.join('; ')}`, 10000);
    if (fileIds.length >= 2) {
      addToast('success', `Radar network: opened ${fileIds.length} of ${sites.length} radars`);
      window.dispatchEvent(
        new CustomEvent('xradar:mosaic-request', { detail: { fileIds, variable: 'DBZH' } }),
      );
    } else if (fileIds.length === 1) {
      addToast('warning', 'Radar network: only one radar opened — a mosaic needs at least two');
    }
  }
</script>

<CollapsiblePanel title="Cloud Data (AWS)" icon="☁">
  <div class="cdp-root">
    <!-- Mode tabs -->
    <div class="cdp-tabs">
      <button class="cdp-tab" class:active={mode === 'realtime'} on:click={() => { mode = 'realtime'; }}>Real-time</button>
      <button class="cdp-tab" class:active={mode === 'archive'} on:click={() => { mode = 'archive'; }}>Archive</button>
      <button class="cdp-tab" class:active={mode === 'arco'} on:click={() => { mode = 'arco'; }}>ARCO</button>
      <button class="cdp-tab" class:active={mode === 'network'} data-testid="cdp-tab-network" on:click={() => { mode = 'network'; }}>Network</button>
    </div>

    <!-- Station row -->
    <div class="cdp-row">
      <label class="cdp-label">{mode === 'network' ? 'Centre' : 'Station'}</label>
      <div class="cdp-station-row">
        {#if stations.length > 0}
          <select class="cdp-select" bind:value={selectedStation}>
            {#each stations as s}
              <option value={s.id}>{s.id}{s.arco ? ' ★' : ''}</option>
            {/each}
          </select>
        {:else}
          <input class="cdp-input" bind:value={selectedStation} placeholder="e.g. KLOT" maxlength="4" />
        {/if}
        <button class="cdp-btn" on:click={fetchStations} disabled={!isConnected || loadingStations} title="Load full station list">
          {loadingStations ? '…' : '⟳'}
        </button>
      </div>
    </div>

    <!-- Real-time mode -->
    {#if mode === 'realtime'}
      <div class="cdp-section">
        <p class="cdp-hint">Opens the most recent NEXRAD Level II scan from <code>unidata-nexrad-level2</code> on AWS S3.</p>
        <button class="cdp-btn primary full" on:click={openRealtime} disabled={!isConnected || isOpening || netBusy || !selectedStation.trim()} aria-busy={openKey?.startsWith('realtime:') ?? false}>
          {openKey?.startsWith('realtime:') ? 'Opening…' : `Open Latest ${selectedStation}`}
        </button>
        {#if openKey?.startsWith('realtime:')}
          <p class="cdp-hint">Downloading from AWS. Progress and Cancel are in the operations tray (status bar).</p>
        {/if}
      </div>
    {/if}

    <!-- Archive mode -->
    {#if mode === 'archive'}
      <div class="cdp-section">
        <div class="cdp-row">
          <label class="cdp-label">Date</label>
          <input type="date" class="cdp-input flex1" bind:value={archiveDate} />
        </div>
        <button class="cdp-btn full" on:click={fetchL2Files} disabled={!isConnected || loadingFiles || !selectedStation}>
          {loadingFiles ? 'Loading…' : 'List Files'}
        </button>

        {#if l2Files.length > 0}
          <div class="cdp-file-list">
            {#each l2Files as file}
              <button class="cdp-file-row" on:click={() => openL2File(file)} disabled={isOpening}>
                <span class="cdp-file-name">{file.name.slice(-19)}</span>
                <span class="cdp-file-size">{openKey === `l2:${file.path}` ? 'opening…' : formatBytes(file.size)}</span>
              </button>
            {/each}
          </div>
        {:else if !loadingFiles}
          <p class="cdp-empty">No files. Click "List Files" to search.</p>
        {/if}
      </div>
    {/if}

    <!-- ARCO mode -->
    {#if mode === 'arco'}
      <div class="cdp-section">
        <p class="cdp-hint">
          <code>nexrad-arco</code> icechunk store. Each entry is a Volume Coverage Pattern
          (VCP-12, VCP-34…) containing all sweeps for every scan. Opens the latest available scan.
          Currently KLOT (Chicago) only.
        </p>
        <button class="cdp-btn full" on:click={fetchArcoScans} disabled={!isConnected || loadingArco || !selectedStation}>
          {loadingArco ? 'Loading…' : 'List VCPs'}
        </button>

        {#if arcoScans.length > 0}
          <div class="cdp-file-list">
            {#each arcoScans as scan}
              <button class="cdp-file-row" on:click={() => openArcoScan(scan)} disabled={isOpening}>
                <span class="cdp-file-name">{scan.key}</span>
                <span class="cdp-file-size">{openKey === `arco:${selectedStation}:${scan.key}` ? 'opening…' : 'latest'}</span>
              </button>
            {/each}
          </div>
        {:else if !loadingArco}
          <p class="cdp-empty">No VCPs loaded. Click "List VCPs".</p>
        {/if}
      </div>
    {/if}

    <!-- Radar network mode -->
    {#if mode === 'network'}
      <div class="cdp-section" data-testid="cdp-network">
        <p class="cdp-hint">
          Opens the centre site plus its nearest neighbours at about the same time, then hands
          them to the Mosaic panel.
        </p>
        {#if activeSite && activeSite !== centre}
          <button class="cdp-btn full" data-testid="cdp-net-active" on:click={() => { selectedStation = activeSite; netExcluded = []; }}>
            Use active file's site ({activeSite})
          </button>
        {/if}

        <div class="cdp-row">
          <label class="cdp-label" for="cdp-net-radius">Radius</label>
          <input id="cdp-net-radius" class="cdp-range" type="range" min="50" max="400" step="10" bind:value={netRadius} />
          <span class="cdp-val">{netRadius} km</span>
        </div>
        <div class="cdp-row">
          <label class="cdp-label" for="cdp-net-max">Radars</label>
          <input id="cdp-net-max" class="cdp-range" type="range" min="2" max="6" step="1" bind:value={netMax} />
          <span class="cdp-val">{netMax}</span>
        </div>

        <div class="cdp-row">
          <span class="cdp-label">Time</span>
          <div class="cdp-tabs cdp-subtabs">
            <button class="cdp-tab" class:active={netTimeMode === 'latest'} on:click={() => { netTimeMode = 'latest'; }}>Latest</button>
            <button class="cdp-tab" class:active={netTimeMode === 'time'} on:click={() => { netTimeMode = 'time'; }}>Date &amp; time</button>
          </div>
        </div>
        {#if netTimeMode === 'time'}
          <div class="cdp-row">
            <label class="cdp-label" for="cdp-net-date">UTC</label>
            <input id="cdp-net-date" type="date" class="cdp-input flex1" bind:value={netDate} />
            <input id="cdp-net-time" type="time" class="cdp-input time" bind:value={netTime} aria-label="UTC time" />
          </div>
          {#if !netTimeValid}<p class="cdp-warn">Enter a valid UTC date and time.</p>{/if}
        {/if}

        {#if stations.length === 0}
          <p class="cdp-empty">Station list not loaded. Click ⟳.</p>
        {:else if !centreHasCoords}
          <p class="cdp-warn">
            No coordinates for {centre || 'this station'}. Pick a NEXRAD site; if none have
            coordinates, restart the sidecar and click ⟳.
          </p>
        {:else}
          <div class="cdp-file-list cdp-net-list" data-testid="cdp-net-list">
            <div class="cdp-net-row centre">
              <input type="checkbox" checked disabled aria-label="{centre} (centre)" />
              <span class="cdp-file-name">{centre}</span>
              <span class="cdp-file-size">centre</span>
            </div>
            {#each neighbours as n (n.id)}
              <label class="cdp-net-row" data-testid="cdp-net-nb">
                <input type="checkbox" checked={!netExcluded.includes(n.id)} disabled={netBusy} on:change={() => toggleExcluded(n.id)} />
                <span class="cdp-file-name">{n.id}</span>
                <span class="cdp-file-size">{formatRelative(n)}</span>
              </label>
            {/each}
          </div>
          {#if neighbours.length === 0}
            <p class="cdp-empty">No other radars within {netRadius} km. Increase the radius.</p>
          {/if}
        {/if}

        <button
          class="cdp-btn primary full"
          data-testid="cdp-net-open"
          on:click={openNetwork}
          disabled={!isConnected || netBusy || !netTimeValid || !centreHasCoords || netSelected.length < 2}
        >
          {netBusy ? 'Opening…' : `Open network (${netSelected.length} radars)`}
        </button>
        {#if netBusy}
          <button class="cdp-btn full" data-testid="cdp-net-cancel" on:click={() => { netCancel = true; }}>Cancel remaining</button>
        {/if}

        {#if netRows.length > 0}
          <div class="cdp-net-progress" data-testid="cdp-net-progress">
            {#each netRows as r (r.id)}
              <div class="cdp-net-line {r.state}">{r.text}</div>
            {/each}
          </div>
        {/if}
      </div>
    {/if}
  </div>
</CollapsiblePanel>

<style>
  .cdp-root {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-sm, 6px);
  }

  .cdp-tabs {
    display: flex;
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm, 4px);
    overflow: hidden;
  }

  .cdp-tab {
    flex: 1;
    padding: 5px 0;
    font-size: 11px;
    font-weight: 600;
    background: transparent;
    color: var(--text-secondary);
    border: none;
    border-radius: 0;
    cursor: pointer;
    transition: all 150ms;
  }

  .cdp-tab:not(:last-child) {
    border-right: 1px solid var(--glass-border);
  }

  .cdp-tab.active {
    background: rgba(var(--c-accent-rgb), 0.15);
    color: var(--accent-hover);
  }

  .cdp-row {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm, 6px);
  }

  .cdp-label {
    font-size: 11px;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.03em;
    font-weight: 600;
    min-width: 52px;
    flex-shrink: 0;
  }

  .cdp-station-row {
    display: flex;
    gap: 4px;
    flex: 1;
  }

  .cdp-select {
    flex: 1;
    min-width: 0;
  }

  .cdp-input {
    flex: 1;
    min-width: 0;
    padding: 3px 6px;
    font-size: 11px;
    font-family: var(--font-mono, monospace);
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm, 4px);
    color: var(--text-primary);
  }

  .cdp-input.flex1 { flex: 1; }

  .cdp-section {
    display: flex;
    flex-direction: column;
    gap: var(--spacing-xs, 4px);
  }

  .cdp-hint {
    font-size: 10px;
    color: var(--text-muted);
    line-height: 1.5;
    margin: 0;
  }

  .cdp-hint code {
    font-family: var(--font-mono, monospace);
    font-size: 10px;
    color: var(--accent-hover);
  }

  .cdp-btn {
    padding: 5px 10px;
    font-size: 11px;
    font-weight: 600;
    border-radius: var(--radius-sm, 4px);
    border: 1px solid var(--glass-border);
    background: var(--field-bg);
    color: var(--text-secondary);
    cursor: pointer;
    transition: all 150ms;
    white-space: nowrap;
  }

  .cdp-btn:hover:not(:disabled) {
    background: rgba(var(--c-accent-rgb), 0.1);
    color: var(--text-primary);
  }

  .cdp-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .cdp-btn.primary {
    background: rgba(var(--c-accent-rgb), 0.15);
    border-color: rgba(var(--c-accent-rgb), 0.3);
    color: var(--accent-hover);
  }

  .cdp-btn.primary:hover:not(:disabled) {
    background: rgba(var(--c-accent-rgb), 0.3);
    color: var(--c-text);
  }

  .cdp-btn.full { width: 100%; }

  .cdp-file-list {
    display: flex;
    flex-direction: column;
    gap: 2px;
    max-height: 200px;
    overflow-y: auto;
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm, 4px);
    padding: 2px;
  }

  .cdp-file-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 4px 8px;
    font-size: 10px;
    font-family: var(--font-mono, monospace);
    color: var(--text-secondary);
    background: transparent;
    border: none;
    border-radius: 3px;
    cursor: pointer;
    text-align: left;
    transition: background 120ms;
    height: auto;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }

  .cdp-file-row:hover:not(:disabled) {
    background: rgba(var(--c-accent-rgb), 0.08);
    color: var(--text-primary);
  }

  .cdp-file-row:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .cdp-file-name { flex: 1; }

  .cdp-file-size {
    color: var(--text-muted);
    font-size: 9px;
    margin-left: 6px;
  }

  .cdp-subtabs { flex: 1; }

  .cdp-range { flex: 1; min-width: 0; }

  .cdp-val {
    font-size: 10px;
    font-family: var(--font-mono, monospace);
    color: var(--text-secondary);
    min-width: 42px;
    text-align: right;
  }

  .cdp-input.time { flex: 0 0 96px; }

  .cdp-warn {
    font-size: 10px;
    color: var(--warning);
    line-height: 1.5;
    margin: 0;
  }

  .cdp-net-list { max-height: 180px; }

  .cdp-net-row {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 3px 8px;
    font-size: 10px;
    font-family: var(--font-mono, monospace);
    color: var(--text-secondary);
    border-radius: 3px;
    cursor: pointer;
  }

  .cdp-net-row:hover { background: rgba(var(--c-accent-rgb), 0.08); }
  .cdp-net-row.centre { cursor: default; color: var(--accent-hover); }
  .cdp-net-row input { margin: 0; width: auto; }

  .cdp-net-progress {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 4px 6px;
    border: 1px solid var(--glass-border);
    border-radius: var(--radius-sm, 4px);
  }

  .cdp-net-line {
    font-size: 10px;
    font-family: var(--font-mono, monospace);
    color: var(--text-muted);
    overflow-wrap: anywhere;
  }

  .cdp-net-line.running { color: var(--accent-hover); }
  .cdp-net-line.ok { color: var(--success); }
  .cdp-net-line.failed { color: var(--error); }

  .cdp-empty {
    font-size: 10px;
    color: var(--text-muted);
    text-align: center;
    padding: 8px 0;
    margin: 0;
  }
</style>
