/**
 * Message catalogue helper: `$t('remoteOpen.title')` in a component, `translate(...)` anywhere.
 *
 * No dependency. A catalogue is a flat key -> text object (messages/<locale>.ts). Lookup order:
 * the active locale, then English, then the key itself (a missing translation is never blank and
 * never throws). Texts may contain `{name}` parameters; for a count pass `count` and write
 * `key_one` / `key_other` (the plural category comes from Intl.PluralRules, so other languages
 * can add `_few`, `_many`, ...). Adding a language = one catalogue file registered below.
 */
import { derived, writable } from 'svelte/store';
import { en, type MessageKey } from './messages/en.ts';

export type Catalogue = Record<string, string>;
export type Params = Record<string, string | number>;

const catalogues: Record<string, Catalogue> = { en };

/** Register (or replace) the catalogue of a locale, e.g. from a lazily imported file. */
export function registerLocale(locale: string, messages: Catalogue): void {
  catalogues[locale] = messages;
}

export function availableLocales(): string[] {
  return Object.keys(catalogues);
}

export const locale = writable<string>('en');

function lookup(loc: string, key: string): string | undefined {
  return catalogues[loc]?.[key] ?? catalogues[loc.split('-')[0]]?.[key];
}

function fill(text: string, params?: Params): string {
  if (!params) return text;
  return text.replace(/\{(\w+)\}/g, (whole, name) => (name in params ? String(params[name]) : whole));
}

/** The text of `key` in `loc` (English, then the key itself, when it is missing). */
export function translate(loc: string, key: MessageKey | (string & {}), params?: Params): string {
  let text: string | undefined;
  if (params && typeof params.count === 'number') {
    const category = new Intl.PluralRules(loc).select(params.count);
    text = lookup(loc, `${key}_${category}`) ?? lookup('en', `${key}_${category}`);
  }
  text ??= lookup(loc, key) ?? lookup('en', key) ?? key;
  return fill(text, params);
}

/** Reactive translator for components: `{$t('remoteOpen.cancel')}`. */
export const t = derived(locale, ($locale) => (key: MessageKey | (string & {}), params?: Params) =>
  translate($locale, key, params),
);

/** Keys of `reference` that `messages` lacks and keys it has that `reference` does not (a check for catalogues). */
export function compareCatalogues(
  reference: Catalogue,
  messages: Catalogue,
): { missing: string[]; unknown: string[] } {
  const ref = new Set(Object.keys(reference));
  const got = new Set(Object.keys(messages));
  return {
    missing: [...ref].filter((k) => !got.has(k)),
    unknown: [...got].filter((k) => !ref.has(k)),
  };
}
