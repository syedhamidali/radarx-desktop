/**
 * Open radar networks beyond NEXRAD (DWD, FMI, ...): what the engine's network providers say
 * about a site, and the consent that stands in front of every listing and download.
 *
 * Loaded lazily with the radar network map. Network rule: nothing here reaches a server before
 * the user has answered a consent dialog (the same dialog as the NEXRAD radar sites), and the
 * answer is kept in memory for this session only.
 */
import { writable, get } from 'svelte/store';
import { wsManager } from '../utils/websocket';
import { connectionStatus } from './radarData';
import { requestCloud } from '../utils/cloudRequest';
import { ask as askNexrad, checkLatest as checkNexradLatest, openLatest as openNexradLatest, browseArchive as browseNexradArchive, BUILTIN_SITES } from './radarSites';
import { needsPrompt, resolveChoice, NO_CONSENT, formatBytes, type ConsentState, type ConsentChoice, type ConsentText, type ConsentStep } from '../utils/radarSites';
import type { CatalogueSite } from '../utils/radarCatalogue';

export interface NetworkProviderInfo {
  id: string;
  name: string;
  network: string;
  attribution: string;
  licence: string;
  homepage: string;
  host: string;
  listing_text: string;
  listing_bytes: number;
  archive_only: boolean;
  /** Day the archive picker starts on ("" = today); a static sample names its last day. */
  default_date: string;
  description: string;
  sites: string[];
}

export interface NetworkScan {
  key: string;
  name: string;
  scan_time: string;
  size: number;
  n_files?: number;
  sweeps?: number;
  complete?: boolean;
}

export interface NetworkLatest extends Partial<NetworkScan> {
  status: 'ok' | 'not_found' | 'offline' | 'error';
  provider: string;
  site: string;
  message?: string;
  now?: string;
  age_s?: number;
}

export interface NetworkCheck {
  state: 'loading' | 'ok' | 'not_found' | 'offline' | 'error';
  info?: NetworkLatest;
  receivedAt: number;
  message?: string;
}

/** Provider descriptions from the engine (null until asked). Asking is a local engine call. */
export const providers = writable<Record<string, NetworkProviderInfo> | null>(null);
/** Per "provider:site" answers of this session. */
export const networkChecks = writable<Record<string, NetworkCheck>>({});

const TIMEOUT_MS = 60_000;
let providersPromise: Promise<Record<string, NetworkProviderInfo>> | null = null;

function once<T>(okType: string, match: (m: any) => boolean, errorPrefix: string, send: () => void, timeoutMs = TIMEOUT_MS): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    if (get(connectionStatus) !== 'connected') {
      reject(new Error('The engine is not connected.'));
      return;
    }
    let done = false;
    const unsubs: Array<() => void> = [];
    const finish = (fn: () => void) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      unsubs.forEach((u) => u());
      fn();
    };
    const timer = setTimeout(() => finish(() => reject(new Error('The engine did not answer in time.'))), timeoutMs);
    unsubs.push(wsManager.onMessage(okType, (m: any) => { if (match(m)) finish(() => resolve(m as T)); }));
    unsubs.push(wsManager.onMessage('error', (m: any) => {
      const text = String(m?.message ?? '');
      if (text.startsWith(errorPrefix)) finish(() => reject(new Error(text.replace(/^[a-z_0-9]+ failed:\s*/i, ''))));
    }));
    let first = true;
    unsubs.push(connectionStatus.subscribe((s) => {
      if (first) { first = false; return; }
      if (s !== 'connected') finish(() => reject(new Error('Connection to the engine was lost.')));
    }));
    send();
  });
}

/** The engine's network providers (cached for the session; a local call, no network). */
export function loadProviders(): Promise<Record<string, NetworkProviderInfo>> {
  const known = get(providers);
  if (known) return Promise.resolve(known);
  if (!providersPromise) {
    providersPromise = once<{ providers: NetworkProviderInfo[] }>(
      'network_providers', () => true, 'list_network_providers', () => wsManager.send({ type: 'list_network_providers' }),
    ).then((m) => {
      const map: Record<string, NetworkProviderInfo> = {};
      for (const p of m.providers ?? []) map[p.id] = p;
      providers.set(map);
      return map;
    }).finally(() => { providersPromise = null; });
  }
  return providersPromise;
}

// ── Consent (shared wording with the NEXRAD sites; session memory only) ─────────────────────

export const consent = writable<ConsentState>(NO_CONSENT);

export interface NetworkConsentPrompt {
  step: ConsentStep;
  station: string;
  text: ConsentText;
  answer: (c: ConsentChoice) => void;
}

export const consentPrompt = writable<NetworkConsentPrompt | null>(null);

/** The exact wording of the consent dialog for a provider (pure; tested through the UI). */
export function describeNetworkRequest(step: ConsentStep, p: NetworkProviderInfo, site: string, scan?: NetworkScan): ConsentText {
  const credit = `Credit: ${p.attribution}. Licence: ${p.licence}.`;
  if (step === 'list') {
    return {
      title: `Check the latest scan of ${p.network} ${site}`,
      lines: [p.listing_text, credit],
      host: p.host,
      size: `About ${formatBytes(p.listing_bytes)} at most.`,
    };
  }
  const files = scan?.n_files && scan.n_files > 1 ? `${scan.n_files} files of one volume: ` : 'One volume: ';
  return {
    title: `Download a volume of ${p.network} ${site}`,
    lines: [`${files}${scan?.name ?? 'the listed volume'}.`, 'Saved in a temporary folder, opened like any other file, then removed.', credit],
    host: p.host,
    size: `${formatBytes(scan?.size)} (from the listing).`,
  };
}

export function askNetwork(step: ConsentStep, p: NetworkProviderInfo, site: string, scan?: NetworkScan): Promise<boolean> {
  if (!needsPrompt(get(consent), step)) return Promise.resolve(true);
  return new Promise<boolean>((resolve) => {
    get(consentPrompt)?.answer('cancel');
    const prompt: NetworkConsentPrompt = {
      step,
      station: site,
      text: describeNetworkRequest(step, p, site, scan),
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

// ── Actions ────────────────────────────────────────────────────────────────────────────────

const keyOf = (provider: string, site: string) => `${provider}:${site}`;
const inflight = new Map<string, Promise<NetworkCheck>>();

function setCheck(key: string, c: NetworkCheck): void {
  networkChecks.update((m) => ({ ...m, [key]: c }));
}

/** Latest volume of a provider site (consent, then one engine call). Resolves the stored answer. */
export async function checkNetworkLatest(provider: string, site: string, force = false): Promise<NetworkCheck | null> {
  const key = keyOf(provider, site);
  const known = get(networkChecks)[key];
  if (!force && known && known.state !== 'loading' && Date.now() - known.receivedAt < 20_000) return known;
  const running = inflight.get(key);
  if (running) return running;
  const p = (await loadProviders())[provider];
  if (!p) return null;
  if (!(await askNetwork('list', p, site))) return null;
  setCheck(key, { state: 'loading', receivedAt: Date.now() });
  const run = once<NetworkLatest>(
    'network_latest_info',
    (m) => m.provider === provider && m.site === site,
    'get_network_latest_info',
    () => wsManager.send({ type: 'get_network_latest_info', provider, site }),
  ).then((info): NetworkCheck => {
    const c: NetworkCheck = { state: info.status, info, receivedAt: Date.now(), message: info.message };
    setCheck(key, c);
    return c;
  }, (e): NetworkCheck => {
    const c: NetworkCheck = { state: 'error', receivedAt: Date.now(), message: (e as Error).message };
    setCheck(key, c);
    return c;
  }).finally(() => inflight.delete(key));
  inflight.set(key, run);
  return run;
}

/** Volumes of one UTC day (consent for the listing, then one engine call). */
export async function listNetworkScans(provider: string, site: string, date: string): Promise<NetworkScan[]> {
  const p = (await loadProviders())[provider];
  if (!p) throw new Error(`Unknown provider ${provider}`);
  if (!(await askNetwork('list', p, site))) return [];
  const m = await once<{ scans: NetworkScan[] }>(
    'network_scans',
    (r) => r.provider === provider && r.site === site && r.date === date,
    'list_network_scans',
    () => wsManager.send({ type: 'list_network_scans', provider, site, date }),
  );
  return m.scans ?? [];
}

/** Open one listed volume: consent with its real size, then hand over to the Cloud panel. */
export async function openNetworkScan(provider: string, site: string, scan: NetworkScan): Promise<boolean> {
  const p = (await loadProviders())[provider];
  if (!p) return false;
  if (!(await askNetwork('download', p, site, scan))) return false;
  requestCloud({ action: 'open-network', provider, site, key: scan.key, name: scan.name, size: scan.size });
  return true;
}

/** "Open latest scan": check (consent), then download (consent with the size). */
export async function openNetworkLatest(provider: string, site: string): Promise<boolean> {
  const c = await checkNetworkLatest(provider, site, true);
  const info = c?.info;
  if (!c || c.state !== 'ok' || !info?.key || !info.name) return false;
  return openNetworkScan(provider, site, { key: info.key, name: info.name, scan_time: info.scan_time ?? '', size: info.size ?? 0, n_files: info.n_files, sweeps: info.sweeps, complete: info.complete });
}

// ── One entry point for a catalogue site, whatever its provider ────────────────────────────

/** The NEXRAD site record of the Radar sites layer for a catalogue site, if it is one. */
export function nexradSiteOf(site: CatalogueSite) {
  if (site.provider !== 'nexrad') return null;
  return BUILTIN_SITES.find((s) => s.id === site.provider_site) ?? null;
}

export async function openLatestFor(site: CatalogueSite): Promise<void> {
  if (site.data_access !== 'open-loadable' || !site.provider || !site.provider_site) return;
  const nx = nexradSiteOf(site);
  if (nx) { await openNexradLatest(nx); return; }
  await openNetworkLatest(site.provider, site.provider_site);
}

export async function checkLatestFor(site: CatalogueSite, force = false): Promise<void> {
  if (site.data_access !== 'open-loadable' || !site.provider || !site.provider_site) return;
  const nx = nexradSiteOf(site);
  if (nx) { await checkNexradLatest(nx, force); return; }
  await checkNetworkLatest(site.provider, site.provider_site, force);
}

/** NEXRAD: the Cloud panel's Archive tab. Other providers: the card lists a day itself. */
export async function browseArchiveFor(site: CatalogueSite): Promise<void> {
  const nx = nexradSiteOf(site);
  if (nx) await browseNexradArchive(nx);
}

export { askNexrad };

/** Test/diagnostic hook: forget everything learned this session. */
export function resetNetworkSession(): void {
  networkChecks.set({});
  consent.set(NO_CONSENT);
  consentPrompt.set(null);
}
