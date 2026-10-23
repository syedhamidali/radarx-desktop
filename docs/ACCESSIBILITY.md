# Accessibility and localisation

How the app is checked for accessibility, what the last audit found and fixed, what is not covered,
and the plan for translations. Related: {doc}`DESIGN` (tokens, themes).

## What is checked

`scripts/a11y/audit.mjs` runs [axe-core](https://github.com/dequelabs/axe-core) in headless Chrome against
the web build, for the light and the dark theme, on these screens: the first-run tour, the main window,
Settings, the keyboard-shortcut sheet, About, the command palette and Movie Studio. It also checks every one of
these dialogs for the focus rules: focus moves into the dialog, Tab and Shift+Tab never leave it, Escape closes
it, and focus returns to the control that opened it.

```bash
npm pack axe-core                      # once; unpack it and point AXE_CORE at axe.min.js
RADARX_AUTH_DISABLED=1 npm run dev:web # a local developer server only (see the note below)
AXE_CORE=/path/to/axe.min.js node scripts/a11y/audit.mjs --out a11y-report
```

It writes a screenshot per screen and theme and `report.json`, prints a table, and exits with 1 when a serious or
critical violation is left. `A11Y_SCREENS` and `A11Y_DIALOGS` (JSON) choose other screens and dialogs; the
defaults are documented at the top of the script.

`RADARX_AUTH_DISABLED=1` is the project's developer switch for **local** development servers and tests (it skips
the sign-in screen); the installers do not honour it.

## Result of the audit (axe-core 4.10.3, Chrome headless, no engine connected)

| | before | after |
|---|---|---|
| screens x themes with a violation | 14 of 14 | 0 of 14 |
| serious | 1 (the shortcut list scrolls but cannot be focused), both themes | 0 |
| moderate | landmarks without a distinct name (sidebars), no level-one heading, a second banner and footer inside Movie Studio, heading levels skipping | 0 |
| colour contrast | no violation | no violation |
| dialogs passing the four focus rules | 5 of 5 | 5 of 5 |

What was changed, each the smallest fix:

* The toolbar, both sidebars and the status bar have names (`aria-label`), so a screen reader lists them apart.
* A level-one heading for screen readers (`.sr-only`); each tool panel's title is a level-two heading (the
  button inside stays the accordion control) and the section titles inside panels are level three.
* The shortcut list can be focused and scrolled with the keyboard.
* Movie Studio's header and footer are plain blocks, not a second page banner and footer.
* The colormap editor, the data table and the metadata viewer are modal dialogs that did not trap focus; they
  now use the same `focusTrap` action as every other dialog (focus in, Tab kept inside, focus returned).

## What is not covered

* The audit ran **without an engine**, so screens that need an open file (the panels' contents, the viewers, the
  pipeline editor, the export panel) were not audited, only their closed state. Do the same run with a file open
  before a release.
* axe only finds what a program can detect. Not checked here: reading order with a real screen reader
  (VoiceOver, NVDA), the canvas viewers (the radar picture has no text alternative beyond the data panels), and
  focus order inside long panels. "Test with real tools" stays open.
* Motion: `prefers-reduced-motion` switches off CSS animation and transitions everywhere (`theme.css`).
  Animation driven from script (the loop, the sweep line) was not checked against it.
* Colour-blind-safe palettes: the default reflectivity and velocity scales are the project's own choice and were
  not simulated for colour-vision deficiencies in this pass.

## Localisation: the scaffold and the plan

Nothing is translated yet. The scaffold is `src/lib/i18n/`:

* `messages/en.ts` is the source catalogue (`component.what` keys, English text with `{name}` parameters and
  `_one` / `_other` plural forms).
* `index.ts` has `translate(locale, key, params)`, the `$t('key')` store for components, `registerLocale` and
  `compareCatalogues` (missing and unknown keys against English). A missing translation falls back to English,
  then to the key; it is never blank.
* `RemoteOpenDialog.svelte` is the one component whose text is already in the catalogue.

Plan, in this order, one pull request each:

1. Move the strings of the first-run tour, the toolbar and the dialogs (Settings, About, shortcuts, update) into
   `en.ts`. Do not change an English word while moving it.
2. A language setting (`auto` follows the browser), loading of a catalogue on demand, `lang` on `<html>`.
3. Dates, numbers and units through `Intl` (times stay UTC and labelled as such; units stay SI as the data are).
4. Messages that come from the engine are built in English today; give them stable codes with parameters so the
   app can translate them, keeping the English text as the fallback and for the command line.
5. First languages chosen by who can review them. A catalogue is checked with `compareCatalogues` in a unit test.

The scientific vocabulary (variable names, product names, units) is not translated; labels around it are.
