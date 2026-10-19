/**
 * What the sounding panel shows, as pure functions: number formatting that never hides a missing
 * value, the table rows of the diagnostics (each with its unit, height reference and definition),
 * wind barbs and the date handling of the archive request. No Svelte, no DOM.
 */

export type ParcelKey = 'surface-based' | 'mixed-layer' | 'most-unstable';
export const PARCEL_KEYS: ParcelKey[] = ['surface-based', 'mixed-layer', 'most-unstable'];
export const PARCEL_SHORT: Record<ParcelKey, string> = { 'surface-based': 'SB', 'mixed-layer': 'ML (100 hPa)', 'most-unstable': 'MU (300 hPa)' };

export interface ParcelDiag {
  parcel: ParcelKey;
  label: string;
  cape?: number | null;
  cin?: number | null;
  lcl_agl_m?: number | null;
  lfc_agl_m?: number | null;
  el_agl_m?: number | null;
  lcl_hpa?: number | null;
  lfc_hpa?: number | null;
  el_hpa?: number | null;
  lifted_index?: number | null;
  start?: { pressure_hpa: number | null; temperature_c: number | null; dewpoint_c: number | null };
  note?: string;
  error?: string;
  el_at_top?: boolean;
}

/** "n/a" for a value that could not be computed; never an invented zero. */
export function fmt(v: number | null | undefined, unit = '', digits = 0): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return 'n/a';
  const s = v.toFixed(digits).replace(/^-0(\.0+)?$/, (m) => m.slice(1));
  return unit ? `${s} ${unit}` : s;
}

export interface Row {
  key: string; // key of the definition in the engine's `definitions`
  label: string;
  value: string;
  sub?: string;
}

export function parcelRows(p: ParcelDiag | undefined): Row[] {
  if (!p || p.error) return [];
  return [
    { key: 'cape', label: 'CAPE', value: fmt(p.cape, 'J/kg') },
    { key: 'cin', label: 'CIN', value: fmt(p.cin, 'J/kg') },
    { key: 'lcl', label: 'LCL', value: fmt(p.lcl_agl_m, 'm AGL'), sub: p.lcl_hpa != null ? `${fmt(p.lcl_hpa, 'hPa')}` : undefined },
    { key: 'lfc', label: 'LFC', value: fmt(p.lfc_agl_m, 'm AGL'), sub: p.lfc_hpa != null ? `${fmt(p.lfc_hpa, 'hPa')}` : undefined },
    { key: 'el', label: 'EL', value: fmt(p.el_agl_m, 'm AGL') + (p.el_at_top ? ' (top of sounding)' : ''), sub: p.el_hpa != null ? `${fmt(p.el_hpa, 'hPa')}` : undefined },
    { key: 'lifted_index', label: 'Lifted index', value: fmt(p.lifted_index, 'K', 1) },
  ];
}

export function shearRows(shear: any): Row[] {
  if (!shear) return [];
  return Object.entries(shear).map(([layer, s]: [string, any]) => ({
    key: 'shear',
    label: `Bulk shear ${layer.replace('km', ' km')}`,
    value: fmt(s?.speed_ms, 'm/s', 1),
    sub: s?.speed_kt != null ? fmt(s.speed_kt, 'kt') : undefined,
  }));
}

export function srhRows(srh: any, storm: any): Row[] {
  if (!srh) return [];
  const rows: Row[] = [];
  for (const [layer, s] of Object.entries(srh) as [string, any][]) {
    rows.push({
      key: 'srh',
      label: `SRH ${layer.replace('km', ' km')}`,
      value: fmt(s?.right_mover, 'm²/s²'),
      sub: s?.left_mover != null ? `left mover ${fmt(s.left_mover, 'm²/s²')}` : undefined,
    });
  }
  if (storm?.right_mover) {
    const r = storm.right_mover;
    rows.push({ key: 'bunkers', label: 'Storm motion (Bunkers right)', value: `${fmt(r.from_deg, '°')} at ${fmt(r.speed_ms, 'm/s', 1)}`, sub: `${fmt(r.speed_kt, 'kt')}` });
  }
  return rows;
}

export function levelRows(levels: any, moisture: any): Row[] {
  const rows: Row[] = [];
  if (levels) {
    rows.push({ key: 'freezing', label: 'Freezing level', value: fmt(levels.freezing_agl_m, 'm AGL') });
    rows.push({ key: 'wet_bulb_zero', label: 'Wet-bulb zero', value: fmt(levels.wet_bulb_zero_agl_m, 'm AGL') });
    rows.push({ key: 'freezing', label: '-20 °C level', value: fmt(levels.minus20_agl_m, 'm AGL') });
  }
  if (moisture) {
    rows.push({ key: 'pw', label: 'Precipitable water', value: fmt(moisture.precipitable_water_mm, 'mm', 1), sub: `to ${fmt(moisture.pw_top_hpa, 'hPa')}` });
  }
  return rows;
}

// ── Wind barbs ──────────────────────────────────────────────────────────────────────────────────

/**
 * SVG path of a wind barb at (x, y): the staff points to where the wind blows FROM; pennants (50 kt),
 * long barbs (10 kt) and short barbs (5 kt) hang off the staff end, rotated 60 degrees clockwise from
 * the staff (the Northern Hemisphere convention). Speeds are rounded to 5 kt. Calm = a small circle.
 */
export function barbPath(x: number, y: number, uMs: number, vMs: number, len = 20): string {
  const kt = Math.hypot(uMs, vMs) / 0.514444;
  const r5 = Math.round(kt / 5) * 5;
  if (r5 < 5) return `M${x - 2} ${y}a2 2 0 1 0 4 0a2 2 0 1 0 -4 0`;
  const from = (Math.atan2(-uMs, -vMs) + 2 * Math.PI) % (2 * Math.PI); // compass bearing the wind comes from
  const dir = (b: number): [number, number] => [Math.sin(b), -Math.cos(b)];
  const [sx, sy] = dir(from);
  const [fx, fy] = dir(from + Math.PI / 3);
  const f = len * 0.4;
  let d = `M${x.toFixed(1)} ${y.toFixed(1)}L${(x + sx * len).toFixed(1)} ${(y + sy * len).toFixed(1)}`;
  let pos = len;
  let rest = r5;
  const at = (t: number): [number, number] => [x + sx * t, y + sy * t];
  while (rest >= 50) {
    const [ax, ay] = at(pos);
    const [bx, by] = at(pos - 4);
    d += `M${ax.toFixed(1)} ${ay.toFixed(1)}L${(ax + fx * f).toFixed(1)} ${(ay + fy * f).toFixed(1)}L${bx.toFixed(1)} ${by.toFixed(1)}`;
    pos -= 5;
    rest -= 50;
  }
  while (rest >= 10) {
    const [ax, ay] = at(pos);
    d += `M${ax.toFixed(1)} ${ay.toFixed(1)}L${(ax + fx * f).toFixed(1)} ${(ay + fy * f).toFixed(1)}`;
    pos -= 3;
    rest -= 10;
  }
  if (rest >= 5) {
    if (pos === len) pos -= 3; // a lone short barb is not drawn at the very end
    const [ax, ay] = at(pos);
    d += `M${ax.toFixed(1)} ${ay.toFixed(1)}L${(ax + fx * f * 0.5).toFixed(1)} ${(ay + fy * f * 0.5).toFixed(1)}`;
  }
  return d;
}

// ── Archive request ─────────────────────────────────────────────────────────────────────────────

/** `datetime-local`-style "2022-03-30T00:00" (taken as UTC) to the engine's "...:00Z"; null if invalid. */
export function launchTimeUtc(text: string): string | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::\d{2})?$/.exec(text.trim());
  if (!m) return null;
  const [, y, mo, d, h, mi] = m.map(Number) as unknown as number[];
  const dt = new Date(Date.UTC(y, mo - 1, d, h, mi, 0));
  if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== mo - 1 || dt.getUTCDate() !== d || dt.getUTCHours() !== h) return null;
  return dt.toISOString().replace('.000Z', 'Z');
}

/** A station id the archive can use: 3 to 12 letters or digits. */
export const validStation = (s: string): boolean => /^[A-Za-z0-9]{3,12}$/.test(s.trim());
