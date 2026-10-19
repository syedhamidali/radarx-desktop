<script lang="ts">
  import { onMount, onDestroy, untrack } from 'svelte';
  import { wsManager } from '../utils/websocket';
  import { portal } from '../utils/portal';
  import { selectedVariable, selectedSweep, radarData } from '../stores/radarData';

  let { visible = $bindable(false) }: { visible?: boolean } = $props();

  // Data state
  let azimuths = $state<number[]>([]);
  /** What the first column holds: 'azimuth' for a PPI, 'elevation' for an RHI sweep. */
  let rayAxis = $state<'azimuth' | 'elevation'>('azimuth');
  let ranges = $state<number[]>([]);
  let values = $state<number[][]>([]);
  let loading = $state(false);
  let errorMsg = $state('');

  // Virtual scroll state
  let containerEl = $state<HTMLDivElement>(undefined as any);
  let scrollTop = $state(0);
  let scrollLeft = $state(0);
  const ROW_HEIGHT = 28;
  const COL_WIDTH = 80;
  const HEADER_HEIGHT = 32;
  const ROW_HEADER_WIDTH = 72;

  // Window request params
  let windowAzStart = $state(0);
  let windowAzEnd = $state(100);
  let windowRangeStart = $state(0);
  let windowRangeEnd = $state(100);
  let totalAz = $state(0);
  let totalRange = $state(0);

  // Selection state
  let selectedCell = $state<{ row: number; col: number } | null>(null);
  let dragStart = $state<{ row: number; col: number } | null>(null);
  let dragEnd = $state<{ row: number; col: number } | null>(null);
  let isDragging = $state(false);

  // Sort
  let sortCol = $state<number | null>(null);
  let sortAsc = $state(true);

  // Filter
  let hideNanRows = $state(false);
  let hideNanCols = $state(false);

  // Search
  let searchMin = $state('');
  let searchMax = $state('');
  let searchResults = $state<{ row: number; col: number }[]>([]);
  let searchResultIdx = $state(-1);

  // Status bar
  let statusText = $state('');

  const variable = $derived($selectedVariable);
  const sweep = $derived($selectedSweep);
  const data = $derived($radarData);

  // NEXRAD stores "no echo" reflectivity as -33 dBZ (a real number, not NaN) and some writers use
  // -9999: both are no-data, not measurements. Treating them as values flattened the colour scale
  // and made "Hide NaN" do nothing.
  const NODATA_FLOOR_DBZ = -33;
  const reflectivityLike = $derived(/^(dbz|reflectivity|corrected_reflectivity|total_power)/i.test(variable ?? ''));
  function isNoData(v: number | null | undefined): boolean {
    if (v === null || v === undefined || isNaN(v) || v === -9999) return true;
    return reflectivityLike && v <= NODATA_FLOOR_DBZ;
  }

  // The sidecar returns one window of the sweep at a time (rays x gates).
  const AZ_PAGE = 100;
  const RNG_PAGE = 100;
  let lastKey = '';

  $effect(() => {
    if (visible && variable && data.variables.length > 0) {
      const key = `${data.filePath}|${variable}|${sweep}`;
      untrack(() => {
        if (key !== lastKey) {
          // New file / variable / sweep: start from the first window, drop selection from the old one
          lastKey = key;
          windowAzStart = 0;
          windowAzEnd = AZ_PAGE;
          windowRangeStart = 0;
          windowRangeEnd = RNG_PAGE;
          clearSelection();
        }
      });
      requestDataWindow();
    }
  });

  function clearSelection() {
    selectedCell = null;
    dragStart = null;
    dragEnd = null;
    searchResults = [];
    searchResultIdx = -1;
    sortCol = null;
  }

  function moveWindow(axis: 'az' | 'rng', start: number) {
    const total = axis === 'az' ? totalAz : totalRange;
    const page = axis === 'az' ? AZ_PAGE : RNG_PAGE;
    const s = Math.max(0, Math.min(Math.max(0, total - page), Math.round(start) || 0));
    if (axis === 'az') {
      if (s === windowAzStart) return;
      windowAzStart = s;
      windowAzEnd = s + page;
    } else {
      if (s === windowRangeStart) return;
      windowRangeStart = s;
      windowRangeEnd = s + page;
    }
    clearSelection();
  }

  // Computed display data (with filters and sort applied)
  const displayRows = $derived(computeDisplayRows(values, azimuths, ranges, hideNanRows, hideNanCols, sortCol, sortAsc));
  const displayCols = $derived(computeDisplayCols(ranges, values, hideNanCols));
  const visibleRowRange = $derived(computeVisibleRows(scrollTop, displayRows.length));
  const visibleColRange = $derived(computeVisibleCols(scrollLeft, displayCols.length));
  const totalContentHeight = $derived(displayRows.length * ROW_HEIGHT);
  const totalContentWidth = $derived(displayCols.length * COL_WIDTH);

  interface DisplayRow {
    origIdx: number;
    azimuth: number;
    values: number[];
  }

  function computeDisplayRows(
    vals: number[][], az: number[], rng: number[],
    hideNan: boolean, hideNanC: boolean,
    sCol: number | null, sAsc: boolean
  ): DisplayRow[] {
    if (!vals.length) return [];
    let rows: DisplayRow[] = az.map((a, i) => ({
      origIdx: i,
      azimuth: a,
      values: vals[i] || [],
    }));
    if (hideNan) {
      rows = rows.filter(r => r.values.some(v => !isNoData(v)));
    }
    if (sCol !== null && sCol >= 0 && sCol < (rng.length || 0)) {
      const colIdx = sCol;
      // No-data cells always sort last, whichever the direction
      rows.sort((a, b) => {
        const na = isNoData(a.values[colIdx]);
        const nb = isNoData(b.values[colIdx]);
        if (na || nb) return na === nb ? 0 : na ? 1 : -1;
        const d = a.values[colIdx] - b.values[colIdx];
        return sAsc ? d : -d;
      });
    }
    return rows;
  }

  function computeDisplayCols(rng: number[], vals: number[][], hideNanC: boolean): number[] {
    if (!rng.length) return [];
    let indices = rng.map((_, i) => i);
    if (hideNanC && vals.length > 0) {
      indices = indices.filter(ci => vals.some(row => !isNoData(row[ci])));
    }
    return indices;
  }

  function computeVisibleRows(st: number, total: number): { start: number; end: number } {
    const start = Math.max(0, Math.floor(st / ROW_HEIGHT) - 2);
    const visibleCount = containerEl ? Math.ceil(containerEl.clientHeight / ROW_HEIGHT) + 4 : 30;
    const end = Math.min(total, start + visibleCount);
    return { start, end };
  }

  function computeVisibleCols(sl: number, total: number): { start: number; end: number } {
    const start = Math.max(0, Math.floor(sl / COL_WIDTH) - 1);
    const visibleCount = containerEl ? Math.ceil(containerEl.clientWidth / COL_WIDTH) + 2 : 15;
    const end = Math.min(total, start + visibleCount);
    return { start, end };
  }

  function requestDataWindow() {
    if (!variable) return;
    loading = true;
    errorMsg = '';
    wsManager.send({
      type: 'get_data_table',
      variable,
      sweep,
      az_start: windowAzStart,
      az_end: windowAzEnd,
      range_start: windowRangeStart,
      range_end: windowRangeEnd,
    });
  }

  let unsubDataTable: (() => void) | null = null;

  onMount(() => {
    unsubDataTable = wsManager.onMessage('data_table_result', (msg: any) => {
      loading = false;
      if (msg.error) {
        errorMsg = msg.error;
        return;
      }
      azimuths = msg.azimuths || [];
      rayAxis = msg.ray_axis === 'elevation' ? 'elevation' : 'azimuth';
      ranges = msg.ranges || [];
      values = msg.values || [];
      totalAz = msg.total_az || azimuths.length;
      totalRange = msg.total_range || ranges.length;
      statusText = `Rays ${windowAzStart + 1}-${windowAzStart + azimuths.length} of ${totalAz} | Gates ${windowRangeStart + 1}-${windowRangeStart + ranges.length} of ${totalRange}`;
    });
  });

  onDestroy(() => {
    window.removeEventListener('mouseup', onCellMouseUp);
    if (unsubDataTable) unsubDataTable();
  });

  function onScroll() {
    if (!containerEl) return;
    scrollTop = containerEl.scrollTop;
    scrollLeft = containerEl.scrollLeft;
  }

  function cellValue(row: DisplayRow, colIdx: number): string {
    const ci = displayCols[colIdx];
    if (ci === undefined) return '';
    const v = row.values[ci];
    if (isNoData(v)) return v !== null && v !== undefined && !isNaN(v) && v !== -9999 ? 'no echo' : 'NaN';
    return v.toFixed(2);
  }

  // Colour scale for the cell backgrounds: computed once per data window (this used to be
  // recomputed -- flattening the whole window -- for every visible cell on every render).
  const colorRange = $derived.by(() => computeColorRange(values));

  function computeColorRange(vals: number[][]): { min: number; max: number; hasData: boolean } {
    // Same definition as before: min/max over the first 1000 valid values (row-major).
    let min = Infinity;
    let max = -Infinity;
    let n = 0;
    outer:
    for (const row of vals) {
      for (const x of row) {
        if (isNoData(x)) continue;
        if (x < min) min = x;
        if (x > max) max = x;
        if (++n >= 1000) break outer;
      }
    }
    return { min, max, hasData: n > 0 };
  }

  function cellColor(row: DisplayRow, colIdx: number): string {
    const ci = displayCols[colIdx];
    if (ci === undefined) return 'transparent';
    const v = row.values[ci];
    if (isNoData(v)) return 'transparent';
    // Simple blue-to-red colormap based on position in data range
    const { min, max, hasData } = colorRange;
    if (!hasData) return 'transparent';
    if (max === min) return 'rgba(91, 108, 247, 0.2)';
    const t = Math.max(0, Math.min(1, (v - min) / (max - min)));
    const r = Math.round(t * 255);
    const b = Math.round((1 - t) * 255);
    return `rgba(${r}, ${Math.round(80 + (1 - Math.abs(t - 0.5) * 2) * 100)}, ${b}, 0.3)`;
  }

  function onCellClick(rowIdx: number, colIdx: number) {
    selectedCell = { row: rowIdx, col: colIdx };
    dragStart = null;
    dragEnd = null;
    const row = displayRows[rowIdx];
    if (!row) return;
    const ci = displayCols[colIdx];
    const v = row.values[ci];
    const az = row.azimuth;
    const rng = ranges[ci];
    statusText = `Az: ${az?.toFixed(2)}deg | Range: ${(rng / 1000)?.toFixed(2)} km | Value: ${isNoData(v) ? (v === -9999 || v === null || isNaN(v) ? 'NaN' : `${v} (no echo)`) : v?.toFixed(4)}`;
  }

  function onCellMouseDown(rowIdx: number, colIdx: number, e: MouseEvent) {
    if (e.button !== 0) return;
    // No text selection / native drag while rubber-banding cells, and finish the selection even if
    // the button is released outside the grid (otherwise the drag stays "stuck" on).
    e.preventDefault();
    window.addEventListener('mouseup', onCellMouseUp, { once: true });
    isDragging = true;
    dragStart = { row: rowIdx, col: colIdx };
    dragEnd = { row: rowIdx, col: colIdx };
    selectedCell = null;
  }

  function onCellMouseMove(rowIdx: number, colIdx: number) {
    if (!isDragging || !dragStart) return;
    dragEnd = { row: rowIdx, col: colIdx };
  }

  function onCellMouseUp() {
    if (!isDragging || !dragStart || !dragEnd) {
      isDragging = false;
      return;
    }
    isDragging = false;
    // If single cell, treat as click
    if (dragStart.row === dragEnd.row && dragStart.col === dragEnd.col) {
      onCellClick(dragStart.row, dragStart.col);
      return;
    }
    // Compute stats for selection
    computeSelectionStats();
  }

  function computeSelectionStats() {
    if (!dragStart || !dragEnd) return;
    const r1 = Math.min(dragStart.row, dragEnd.row);
    const r2 = Math.max(dragStart.row, dragEnd.row);
    const c1 = Math.min(dragStart.col, dragEnd.col);
    const c2 = Math.max(dragStart.col, dragEnd.col);
    const vals: number[] = [];
    for (let r = r1; r <= r2; r++) {
      const row = displayRows[r];
      if (!row) continue;
      for (let c = c1; c <= c2; c++) {
        const ci = displayCols[c];
        const v = row.values[ci];
        if (!isNoData(v)) {
          vals.push(v);
        }
      }
    }
    if (vals.length === 0) {
      statusText = `Selection: ${r2 - r1 + 1} x ${c2 - c1 + 1} | no valid values`;
      return;
    }
    const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    statusText = `Selection: ${r2 - r1 + 1} x ${c2 - c1 + 1} | Count: ${vals.length} | Mean: ${mean.toFixed(3)} | Min: ${min.toFixed(3)} | Max: ${max.toFixed(3)}`;
  }

  function isCellSelected(rowIdx: number, colIdx: number): boolean {
    if (selectedCell && selectedCell.row === rowIdx && selectedCell.col === colIdx) return true;
    if (dragStart && dragEnd) {
      const r1 = Math.min(dragStart.row, dragEnd.row);
      const r2 = Math.max(dragStart.row, dragEnd.row);
      const c1 = Math.min(dragStart.col, dragEnd.col);
      const c2 = Math.max(dragStart.col, dragEnd.col);
      return rowIdx >= r1 && rowIdx <= r2 && colIdx >= c1 && colIdx <= c2;
    }
    return false;
  }

  function isCellSearchResult(rowIdx: number, colIdx: number): boolean {
    return searchResults.some(r => r.row === rowIdx && r.col === colIdx);
  }

  function toggleSort(colIdx: number) {
    if (sortCol === colIdx) {
      sortAsc = !sortAsc;
    } else {
      sortCol = colIdx;
      sortAsc = true;
    }
  }

  function doSearch() {
    searchResults = [];
    searchResultIdx = -1;
    const minV = searchMin !== '' ? parseFloat(searchMin) : -Infinity;
    const maxV = searchMax !== '' ? parseFloat(searchMax) : Infinity;
    if (isNaN(minV) || isNaN(maxV)) return;
    for (let r = 0; r < displayRows.length; r++) {
      const row = displayRows[r];
      for (let c = 0; c < displayCols.length; c++) {
        const ci = displayCols[c];
        const v = row.values[ci];
        if (!isNoData(v) && v >= minV && v <= maxV) {
          searchResults.push({ row: r, col: c });
        }
      }
    }
    if (searchResults.length > 0) {
      searchResultIdx = 0;
      scrollToCell(searchResults[0]);
      statusText = `Found ${searchResults.length} matching cells`;
    } else {
      statusText = 'No matching cells found';
    }
  }

  function nextSearchResult() {
    if (searchResults.length === 0) return;
    searchResultIdx = (searchResultIdx + 1) % searchResults.length;
    scrollToCell(searchResults[searchResultIdx]);
  }

  function prevSearchResult() {
    if (searchResults.length === 0) return;
    searchResultIdx = (searchResultIdx - 1 + searchResults.length) % searchResults.length;
    scrollToCell(searchResults[searchResultIdx]);
  }

  function scrollToCell(cell: { row: number; col: number }) {
    if (!containerEl) return;
    containerEl.scrollTop = cell.row * ROW_HEIGHT;
    containerEl.scrollLeft = cell.col * COL_WIDTH;
  }

  /** Selected cell range as [r1, r2, c1, c2] in display indices, or null. */
  function selectionBounds(): [number, number, number, number] | null {
    if (dragStart && dragEnd) {
      return [
        Math.min(dragStart.row, dragEnd.row), Math.max(dragStart.row, dragEnd.row),
        Math.min(dragStart.col, dragEnd.col), Math.max(dragStart.col, dragEnd.col),
      ];
    }
    if (selectedCell) return [selectedCell.row, selectedCell.row, selectedCell.col, selectedCell.col];
    return null;
  }

  /** Tab-separated copy of the selection (azimuth column + range header in km), NaN as empty. */
  function selectionTSV(): string {
    const b = selectionBounds();
    if (!b) return '';
    const [r1, r2, c1, c2] = b;
    const head = ['azimuth_deg \\ range_km'];
    for (let c = c1; c <= c2; c++) head.push((ranges[displayCols[c]] / 1000).toFixed(3));
    const lines = [head.join('\t')];
    for (let r = r1; r <= r2; r++) {
      const row = displayRows[r];
      if (!row) continue;
      const cells = [row.azimuth.toFixed(2)];
      for (let c = c1; c <= c2; c++) {
        const v = row.values[displayCols[c]];
        cells.push(v === null || v === undefined || isNaN(v) || v === -9999 ? '' : String(v)); // raw values, no-echo kept
      }
      lines.push(cells.join('\t'));
    }
    return lines.join('\n');
  }

  async function copySelection() {
    const text = selectionTSV();
    if (!text) {
      statusText = 'Select cells first (click or drag), then copy';
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      const b = selectionBounds()!;
      statusText = `Copied ${b[1] - b[0] + 1} x ${b[3] - b[2] + 1} cells to the clipboard`;
    } catch {
      statusText = 'Copy failed: clipboard access was denied';
    }
  }

  function onKeydown(e: KeyboardEvent) {
    if (!visible) return;
    if (e.key === 'Escape') {
      e.stopPropagation();
      close();
    } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'c') {
      // Leave copying alone while a text input (search box) has the caret
      if ((e.target as HTMLElement)?.tagName === 'INPUT') return;
      e.preventDefault();
      copySelection();
    }
  }

  function close() {
    visible = false;
  }
</script>

<svelte:window on:keydown={onKeydown} />

{#if visible}
<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="data-table-overlay" use:portal on:click|self={close}>
  <div class="data-table-panel" role="dialog" aria-modal="true" aria-label="Data table">
    <div class="dt-header">
      <div class="dt-title">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <rect x="3" y="3" width="18" height="18" rx="2" />
          <line x1="3" y1="9" x2="21" y2="9" />
          <line x1="3" y1="15" x2="21" y2="15" />
          <line x1="9" y1="3" x2="9" y2="21" />
          <line x1="15" y1="3" x2="15" y2="21" />
        </svg>
        <span>Data Table — {variable || 'none'} (sweep {sweep})</span>
      </div>
      <div class="dt-actions">
        <button class="dt-btn" on:click={close} title="Close">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
      </div>
    </div>

    <div class="dt-toolbar">
      <label class="dt-filter">
        <input type="checkbox" bind:checked={hideNanRows} />
        <span>Hide NaN rows</span>
      </label>
      <label class="dt-filter">
        <input type="checkbox" bind:checked={hideNanCols} />
        <span>Hide NaN cols</span>
      </label>
      <div class="dt-separator"></div>
      <div class="dt-pager" title="The table shows one {AZ_PAGE} x {RNG_PAGE} window of the sweep at a time">
        <span class="dt-search-dash">Ray</span>
        <button class="dt-btn-sm" on:click={() => moveWindow('az', windowAzStart - AZ_PAGE)} disabled={windowAzStart <= 0} title="Previous {AZ_PAGE} rays" aria-label="Previous rays">&lsaquo;</button>
        <input class="dt-search-input" type="number" min="0" max={Math.max(0, totalAz - 1)}
               value={windowAzStart} aria-label="First ray index"
               on:change={(e) => moveWindow('az', Number(e.currentTarget.value))} />
        <button class="dt-btn-sm" on:click={() => moveWindow('az', windowAzStart + AZ_PAGE)} disabled={windowAzStart + AZ_PAGE >= totalAz} title="Next {AZ_PAGE} rays" aria-label="Next rays">&rsaquo;</button>
        <span class="dt-search-dash">Gate</span>
        <button class="dt-btn-sm" on:click={() => moveWindow('rng', windowRangeStart - RNG_PAGE)} disabled={windowRangeStart <= 0} title="Previous {RNG_PAGE} gates" aria-label="Previous gates">&lsaquo;</button>
        <input class="dt-search-input" type="number" min="0" max={Math.max(0, totalRange - 1)}
               value={windowRangeStart} aria-label="First gate index"
               on:change={(e) => moveWindow('rng', Number(e.currentTarget.value))} />
        <button class="dt-btn-sm" on:click={() => moveWindow('rng', windowRangeStart + RNG_PAGE)} disabled={windowRangeStart + RNG_PAGE >= totalRange} title="Next {RNG_PAGE} gates" aria-label="Next gates">&rsaquo;</button>
      </div>
      <button class="dt-btn-sm" on:click={copySelection} title="Copy selected cells as tab-separated text (Ctrl/Cmd+C)">Copy</button>
      <div class="dt-separator"></div>
      <div class="dt-search">
        <input type="text" placeholder="Min" bind:value={searchMin} class="dt-search-input" />
        <span class="dt-search-dash">-</span>
        <input type="text" placeholder="Max" bind:value={searchMax} class="dt-search-input" />
        <button class="dt-btn-sm" on:click={doSearch}>Search</button>
        {#if searchResults.length > 0}
          <button class="dt-btn-sm" on:click={prevSearchResult} title="Previous">&lt;</button>
          <span class="dt-search-count">{searchResultIdx + 1}/{searchResults.length}</span>
          <button class="dt-btn-sm" on:click={nextSearchResult} title="Next">&gt;</button>
        {/if}
      </div>
    </div>

    {#if loading}
      <div class="dt-loading">
        <div class="dt-spinner"></div>
        <span>Loading data...</span>
      </div>
    {:else if errorMsg}
      <div class="dt-error">{errorMsg}</div>
    {:else if displayRows.length === 0}
      <div class="dt-empty">No data available. Open a file and select a variable.</div>
    {:else}
      <div class="dt-table-wrapper">
        <!-- Fixed corner -->
        <div class="dt-corner">{rayAxis === 'elevation' ? 'El' : 'Az'} \ Range</div>

        <!-- Fixed column headers -->
        <div class="dt-col-headers" style="left: {ROW_HEADER_WIDTH}px; transform: translateX(-{scrollLeft}px);">
          {#each { length: visibleColRange.end - visibleColRange.start } as _, i}
            {@const colIdx = visibleColRange.start + i}
            {@const ci = displayCols[colIdx]}
            <div
              class="dt-col-header"
              class:sorted={sortCol === ci}
              style="left: {colIdx * COL_WIDTH}px; width: {COL_WIDTH}px;"
              on:click={() => toggleSort(ci)}
              title="Range: {(ranges[ci] / 1000).toFixed(2)} km — Click to sort"
            >
              {(ranges[ci] / 1000).toFixed(1)}
              {#if sortCol === ci}
                <span class="sort-arrow">{sortAsc ? '▲' : '▼'}</span>
              {/if}
            </div>
          {/each}
        </div>

        <!-- Fixed row headers -->
        <div class="dt-row-headers" style="top: {HEADER_HEIGHT}px; transform: translateY(-{scrollTop}px);">
          {#each { length: visibleRowRange.end - visibleRowRange.start } as _, i}
            {@const rowIdx = visibleRowRange.start + i}
            {@const row = displayRows[rowIdx]}
            {#if row}
              <div class="dt-row-header" style="top: {rowIdx * ROW_HEIGHT}px; height: {ROW_HEIGHT}px;">
                {row.azimuth.toFixed(1)}
              </div>
            {/if}
          {/each}
        </div>

        <!-- Scrollable data area -->
        <div
          class="dt-scroll-area"
          bind:this={containerEl}
          on:scroll={onScroll}
          style="top: {HEADER_HEIGHT}px; left: {ROW_HEADER_WIDTH}px;"
        >
          <div class="dt-scroll-content" style="width: {totalContentWidth}px; height: {totalContentHeight}px;">
            {#each { length: visibleRowRange.end - visibleRowRange.start } as _, ri}
              {@const rowIdx = visibleRowRange.start + ri}
              {@const row = displayRows[rowIdx]}
              {#if row}
                {#each { length: visibleColRange.end - visibleColRange.start } as _, ci}
                  {@const colIdx = visibleColRange.start + ci}
                  <div
                    class="dt-cell"
                    class:selected={isCellSelected(rowIdx, colIdx)}
                    class:search-hit={isCellSearchResult(rowIdx, colIdx)}
                    class:nodata={isNoData(row.values[displayCols[colIdx]])}
                    style="
                      left: {colIdx * COL_WIDTH}px;
                      top: {rowIdx * ROW_HEIGHT}px;
                      width: {COL_WIDTH}px;
                      height: {ROW_HEIGHT}px;
                      background: {cellColor(row, colIdx)};
                    "
                    on:mousedown={(e) => onCellMouseDown(rowIdx, colIdx, e)}
                    on:mousemove={() => onCellMouseMove(rowIdx, colIdx)}
                    on:click={() => onCellClick(rowIdx, colIdx)}
                  >
                    {cellValue(row, colIdx)}
                  </div>
                {/each}
              {/if}
            {/each}
          </div>
        </div>
      </div>
    {/if}

    <div class="dt-status-bar">
      {statusText}
    </div>
  </div>
</div>
{/if}

<style>
  .data-table-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.6);
    backdrop-filter: blur(4px);
    -webkit-backdrop-filter: blur(4px);
    z-index: 1000;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .data-table-panel {
    width: 90vw;
    height: 80vh;
    max-width: 1400px;
    background: var(--bg-primary, #0d1117);
    border: 1px solid var(--glass-border, rgba(255, 255, 255, 0.08));
    border-radius: var(--radius-lg, 12px);
    display: flex;
    flex-direction: column;
    overflow: hidden;
    box-shadow: 0 24px 80px rgba(0, 0, 0, 0.5);
  }

  .dt-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--spacing-sm, 8px) var(--spacing-md, 12px);
    background: rgba(17, 22, 40, 0.8);
    border-bottom: 1px solid var(--glass-border, rgba(255, 255, 255, 0.08));
    flex-shrink: 0;
  }

  .dt-title {
    display: flex;
    align-items: center;
    gap: var(--spacing-sm, 8px);
    font-size: 13px;
    font-weight: 600;
    color: var(--text-primary, #e6edf3);
  }

  .dt-actions {
    display: flex;
    gap: var(--spacing-xs, 4px);
  }

  .dt-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    padding: 0; /* the global button padding would squeeze the icon to 2px */
    background: transparent;
    border: 1px solid var(--glass-border, rgba(255, 255, 255, 0.08));
    border-radius: var(--radius-sm, 4px);
    color: var(--text-muted, #8b949e);
    cursor: pointer;
    transition: all 120ms ease;
  }

  .dt-btn:hover {
    background: rgba(91, 108, 247, 0.1);
    color: var(--text-primary, #e6edf3);
    border-color: rgba(91, 108, 247, 0.3);
  }

  .dt-toolbar {
    display: flex;
    align-items: center;
    gap: var(--spacing-md, 12px);
    padding: var(--spacing-xs, 4px) var(--spacing-md, 12px);
    background: rgba(17, 22, 40, 0.5);
    border-bottom: 1px solid var(--glass-border, rgba(255, 255, 255, 0.08));
    flex-shrink: 0;
    flex-wrap: wrap;
  }

  .dt-filter {
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 11px;
    color: var(--text-secondary, #c9d1d9);
    cursor: pointer;
    user-select: none;
  }

  .dt-filter input[type="checkbox"] {
    accent-color: var(--accent-primary, #5b6cf7);
  }

  .dt-separator {
    width: 1px;
    height: 18px;
    background: var(--glass-border, rgba(255, 255, 255, 0.08));
  }

  .dt-pager {
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 11px;
  }

  .dt-btn-sm:disabled {
    opacity: 0.4;
    cursor: default;
  }

  .dt-search {
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 11px;
  }

  .dt-search-input {
    width: 60px;
    padding: 2px 6px;
    font-size: 11px;
    font-family: var(--font-mono, monospace);
    background: rgba(0, 0, 0, 0.3);
    border: 1px solid var(--glass-border, rgba(255, 255, 255, 0.08));
    border-radius: var(--radius-sm, 4px);
    color: var(--text-primary, #e6edf3);
    outline: none;
  }

  .dt-search-input:focus {
    border-color: var(--accent-primary, #5b6cf7);
  }

  .dt-search-dash {
    color: var(--text-muted, #8b949e);
  }

  .dt-search-count {
    font-size: 10px;
    color: var(--text-muted, #8b949e);
    font-family: var(--font-mono, monospace);
  }

  .dt-btn-sm {
    height: auto;
    padding: 2px 8px;
    font-size: 10px;
    background: rgba(91, 108, 247, 0.1);
    border: 1px solid rgba(91, 108, 247, 0.2);
    border-radius: var(--radius-sm, 4px);
    color: var(--text-primary, #e6edf3);
    cursor: pointer;
    transition: all 120ms ease;
  }

  .dt-btn-sm:hover {
    background: rgba(91, 108, 247, 0.2);
  }

  .dt-loading, .dt-error, .dt-empty {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-sm, 8px);
    color: var(--text-muted, #8b949e);
    font-size: 13px;
  }

  .dt-error {
    color: var(--error, #f85149);
  }

  .dt-spinner {
    width: 20px;
    height: 20px;
    border: 2px solid rgba(91, 108, 247, 0.2);
    border-top-color: var(--accent-primary, #5b6cf7);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  /* Table layout */
  .dt-table-wrapper {
    flex: 1;
    position: relative;
    overflow: hidden;
    min-height: 0;
  }

  .dt-corner {
    position: absolute;
    top: 0;
    left: 0;
    width: 72px;
    height: 32px;
    background: rgba(17, 22, 40, 0.9);
    border-right: 1px solid var(--glass-border, rgba(255, 255, 255, 0.08));
    border-bottom: 1px solid var(--glass-border, rgba(255, 255, 255, 0.08));
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 9px;
    font-weight: 600;
    color: var(--text-muted, #8b949e);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    z-index: 3;
  }

  .dt-col-headers {
    position: absolute;
    top: 0;
    height: 32px;
    z-index: 2;
    pointer-events: auto;
  }

  .dt-col-header {
    position: absolute;
    top: 0;
    height: 32px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 2px;
    font-size: 10px;
    font-weight: 600;
    font-family: var(--font-mono, monospace);
    color: var(--text-secondary, #c9d1d9);
    background: rgba(17, 22, 40, 0.9);
    border-bottom: 1px solid var(--glass-border, rgba(255, 255, 255, 0.08));
    border-right: 1px solid rgba(255, 255, 255, 0.03);
    cursor: pointer;
    user-select: none;
    transition: background 100ms ease;
  }

  .dt-col-header:hover {
    background: rgba(91, 108, 247, 0.08);
  }

  .dt-col-header.sorted {
    color: var(--accent-primary, #5b6cf7);
    background: rgba(91, 108, 247, 0.06);
  }

  .sort-arrow {
    font-size: 8px;
    color: var(--accent-primary, #5b6cf7);
  }

  .dt-row-headers {
    position: absolute;
    left: 0;
    width: 72px;
    z-index: 2;
    pointer-events: none;
  }

  .dt-row-header {
    position: absolute;
    left: 0;
    width: 72px;
    display: flex;
    align-items: center;
    justify-content: flex-end;
    padding-right: 8px;
    font-size: 10px;
    font-family: var(--font-mono, monospace);
    color: var(--text-secondary, #c9d1d9);
    background: rgba(17, 22, 40, 0.9);
    border-right: 1px solid var(--glass-border, rgba(255, 255, 255, 0.08));
    border-bottom: 1px solid rgba(255, 255, 255, 0.03);
  }

  .dt-scroll-area {
    position: absolute;
    right: 0;
    bottom: 0;
    overflow: auto;
    -webkit-overflow-scrolling: touch;
  }

  .dt-scroll-content {
    position: relative;
  }

  .dt-cell.nodata {
    color: var(--text-muted, #8b949e);
    opacity: 0.7;
  }

  .dt-cell {
    position: absolute;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 10px;
    font-family: var(--font-mono, monospace);
    color: var(--text-primary, #e6edf3);
    border-right: 1px solid rgba(255, 255, 255, 0.03);
    border-bottom: 1px solid rgba(255, 255, 255, 0.03);
    cursor: cell;
    user-select: none;
    transition: box-shadow 80ms ease;
  }

  .dt-cell:hover {
    box-shadow: inset 0 0 0 1px rgba(91, 108, 247, 0.4);
    z-index: 1;
  }

  .dt-cell.selected {
    box-shadow: inset 0 0 0 2px var(--accent-primary, #5b6cf7);
    z-index: 2;
    background-color: rgba(91, 108, 247, 0.15) !important;
  }

  .dt-cell.search-hit {
    box-shadow: inset 0 0 0 2px #e3b341;
    z-index: 2;
  }

  .dt-status-bar {
    padding: var(--spacing-xs, 4px) var(--spacing-md, 12px);
    font-size: 11px;
    font-family: var(--font-mono, monospace);
    color: var(--text-muted, #8b949e);
    background: rgba(17, 22, 40, 0.8);
    border-top: 1px solid var(--glass-border, rgba(255, 255, 255, 0.08));
    flex-shrink: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
</style>
