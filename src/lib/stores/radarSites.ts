/**
 * State of the "Radar sites" layer: the catalogue (bundled NEXRAD + the user's own file), the
 * selected site, what the engine said about each site's latest scan, and the network consent.
 *
 * Loaded lazily (the first time the layer is switched on or Settings is opened). The only things
 * the app shell imports eagerly are stores/radarSitesPref.ts and utils/cloudRequest.ts.
 *
 * Network rule: nothing here reaches AWS before `ask()` has been answered "yes" by the user, and
 * the answer is kept in memory for this session only (never persisted).
 */
import { writable, derived, get } from 'svelte/store';
import nexradDoc from '../data/nexradSites.json';
import { wsManager } from '../utils/websocket';
import { connectionStatus } from './radarData';
import { requestCloud } from '../utils/cloudRequest';
import {
  nexradSitesFrom,
  mergeCatalogue,
  isFetchable,
  parseSiteList,
  needsPrompt,
  resolveChoice,
  NO_CONSENT,
  describeRequest,
  type RadarSite,
  type LatestInfo,
  type ConsentState,
  type ConsentStep,
  type ConsentChoice,
  type ConsentText,
  type ParsedSites,
} from '../utils/radarSites';

export const BUILTIN_SITES: RadarSite[] = nexradSitesFrom(nexradDoc as any);
export const BUILTIN_IDS: ReadonlySet<string> = new Set(BUILTIN_SITES.map((s) => s.id.toUpperCase()));
export const BUILTIN_SOURCE = {
  text: String((nexradDoc as any).source ?? 'NOAA NCEI HOMR'),
  retrieved: String((nexradDoc as any).retrieved ?? ''),
  count: BUILTIN_SITES.length,
};

// ── The user's own catalogue (persisted) ────────────────────────────────────────────────────

interface CustomCatalogue {
  sites: RadarSite[];
  fileName: string;
  loadedAt: number;
  rejected: number;
  includeBuiltin: boolean;
}

const CUSTOM_KEY = 'xradar-radar-sites-custom';
const EMPTY_CUSTOM: CustomCatalogue = { sites: [], fileName: '', loadedAt: 0, rejected: 0, includeBuiltin: true };

function loadCustom(): CustomCatalogue {
  try {
    const raw = localStorage.getItem(CUSTOM_KEY);
    if (!raw) return { ...EMPTY_CUSTOM };
    const o = JSON.parse(raw);
    // Re-validate through the parser: stored data is as untrusted as a file.
    const parsed = parseSiteList(JSON.stringify(Array.isArray(o?.sites) ? o.sites : []), 'x.json');
    return {
      sites: parsed.sites,
      fileName: typeof o?.fileName === 'string' ? o.fileName.slice(0, 120) : '',
      loadedAt: Number.isFinite(o?.loadedAt) ? o.loadedAt : 0,
      rejected: Number.isFinite(o?.rejected) ? o.rejected : 0,
      includeBuiltin: o?.includeBuiltin !== false,
    };
  } catch {
    return { ...EMPTY_CUSTOM };
  }
}

export const customCatalogue = writable<CustomCatalogue>(loadCustom());
customCatalogue.subscribe((c) => {
  try {
    localStorage.setItem(CUSTOM_KEY, JSON.stringify(c));
  } catch {
    /* storage full or blocked: the catalogue then lasts for this session only */
  }
});

/** Replace the user's catalogue with a parsed file. */
export function adoptSiteFile(parsed: ParsedSites, fileName: string): void {
  if (parsed.error || parsed.sites.length === 0) return;
  customCatalogue.update((c) => ({
    ...c,
    sites: parsed.sites,
    fileName: fileName.slice(0, 120),
    loadedAt: Date.now(),
    rejected: parsed.rejected.length,
  }));
}

export function clearCustomSites(): void {
  customCatalogue.update((c) => ({ ...c, sites: [], fileName: '', loadedAt: 0, rejected: 0 }));
}

export function setIncludeBuiltin(on: boolean): void {
  customCatalogue.update((c) => ({ ...c, includeBuiltin: on }));
}

/** What the map shows: built-in (unless hidden) plus the user's sites. */
export const catalogue = derived(customCatalogue, (c) => mergeCatalogue(BUILTIN_SITES, c.sites, c.includeBuiltin));

export function siteFetchable(site: RadarSite): boolean {
  return isFetchable(site, BUILTIN_IDS);
}

// ── Selection and per-site answers ──────────────────────────────────────────────────────────

export const selectedSiteId = writable<string | null>(null);

export type CheckState = 'loading' | 'ok' | 'not_found' | 'offline' | 'error';

export interface SiteCheck {
  state: CheckState;
  info?: LatestInfo;
  /** performance-independent wall time of the reply (epoch ms), to age the engine's `age_s`. */
  receivedAt: number;
  message?: string;
}

/** Answers of the engine for this session only. A site that is not in here has state "unknown". */
export const siteChecks = writable<Record<string, SiteCheck>>({});

const INFO_TIMEOUT_MS = 45_000;
const inflight = new Map<string, Promise<SiteCheck>>();

function setCheck(id: string, c: SiteCheck): void {
  siteChecks.update((m) => ({ ...m, [id]: c }));
}

function askEngine(station: string): Promise<LatestInfo> {
  return new Promise((resolve) => {
    if (get(connectionStatus) !== 'connected') {
      resolve({ status: 'error', station, message: 'The engine is not connected.' });
      return;
    }
    let done = false;
    const unsubs: Array<() => void> = [];
    const finish = (v: LatestInfo) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      unsubs.forEach((u) => u());
      resolve(v);
    };
    const timer = setTimeout(
      () => finish({ status: 'error', station, message: 'The engine did not answer in time.' }),
      INFO_TIMEOUT_MS,
    );
    unsubs.push(wsManager.onMessage('nexrad_latest_info', (m: any) => {
      if (String(m?.station ?? '').toUpperCase() === station) finish(m as LatestInfo);
    }));
    unsubs.push(wsManager.onMessage('error', (m: any) => {
      const text = String(m?.message ?? '');
      if (text.startsWith('get_nexrad_latest_info')) finish({ status: 'error', station, message: text });
    }));
    let first = true;
    unsubs.push(connectionStatus.subscribe((s) => {
      if (first) { first = false; return; }
      if (s !== 'connected') finish({ status: 'error', station, message: 'Connection to the engine was lost.' });
    }));
    void wsManager.send({ type: 'get_nexrad_latest_info', station });
  });
}

// ── Consent ─────────────────────────────────────────────────────────────────────────────────

/** Session-only; reset by reloading the app. Never written to storage. */
export const consent = writable<ConsentState>(NO_CONSENT);

export interface ConsentPrompt {
  step: ConsentStep;
  station: string;
  text: ConsentText;
  answer: (c: ConsentChoice) => void;
}

/** The dialog the UI shows (null when none). */
export const consentPrompt = writable<ConsentPrompt | null>(null);

/**
 * Resolve true when the network step may go ahead: at once if the user allowed the session,
 * otherwise after the dialog is answered. A newer prompt cancels an unanswered older one.
 */
export function ask(step: ConsentStep, station: string, file?: { name?: string; size?: number | null }): Promise<boolean> {
  if (!needsPrompt(get(consent), step)) return Promise.resolve(true);
  return new Promise<boolean>((resolve) => {
    get(consentPrompt)?.answer('cancel');
    const prompt: ConsentPrompt = {
      step,
      station,
      text: describeRequest(step, station, new Date(), file),
      answer: (choice) => {
        if (get(consentPrompt) === prompt) consentPrompt.set(null);
        const r = resolveChoice(get(consent), choice);
        consent.set(r.state);
        resolve(r.proceed);
      },
    };
    consentPrompt.set(prompt);
  });
}

// ── Actions of the card ─────────────────────────────────────────────────────────────────────

/** Ask the engine for the newest volume of a site (after consent). Resolves the stored answer. */
export async function checkLatest(site: RadarSite, force = false): Promise<SiteCheck | null> {
  const id = site.id.toUpperCase();
  if (!siteFetchable(site)) return null;
  const known = get(siteChecks)[id];
  if (!force && known && known.state !== 'loading' && Date.now() - known.receivedAt < 20_000) return known;
  const running = inflight.get(id);
  if (running) return running;
  if (!(await ask('list', id))) return null;
  setCheck(id, { state: 'loading', receivedAt: Date.now() });
  const p = askEngine(id).then((info): SiteCheck => {
    const state: CheckState = info.status;
    const check: SiteCheck = { state, info, receivedAt: Date.now(), message: info.message };
    setCheck(id, check);
    return check;
  }).finally(() => inflight.delete(id));
  inflight.set(id, p);
  return p;
}

/** "Open latest volume": list (consent), then confirm the download with its real size, then hand over. */
export async function openLatest(site: RadarSite): Promise<void> {
  const id = site.id.toUpperCase();
  const check = await checkLatest(site, true);
  const info = check?.info;
  if (!check || check.state !== 'ok' || !info?.path || !info.name) return;
  if (!(await ask('download', id, { name: info.name, size: info.size }))) return;
  requestCloud({ action: 'open', station: id, path: info.path, name: info.name, size: info.size });
}

/** "Browse archive": consent for the listing, then the Cloud panel lists today's folder. */
export async function browseArchive(site: RadarSite): Promise<void> {
  const id = site.id.toUpperCase();
  if (!siteFetchable(site)) return;
  if (!(await ask('list', id))) return;
  requestCloud({ action: 'archive', station: id });
}

/** Test/diagnostic hook: forget everything learned this session. */
export function resetSession(): void {
  siteChecks.set({});
  consent.set(NO_CONSENT);
  selectedSiteId.set(null);
  consentPrompt.set(null);
}
