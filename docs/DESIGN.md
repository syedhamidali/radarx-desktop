# Design language

xradar looks like mission-control software for weather radar: a deep space-navy canvas, hairline
cyan borders, tracked small-caps labels and crisp monospace readouts. It is a tool people watch for
hours, so the rule is **clarity first: the style serves the data**. Nothing decorative ever sits
behind or animates over the radar canvas.

Two themes ship, both built from the same tokens:

| Theme | Setting | Use |
| --- | --- | --- |
| **Mission control** | dark (default) | long sessions, low glare |
| **Clean room** | light | print, projectors, bright rooms |

Settings -> General -> Appearance switches between Mission control, Clean room and *System*
(follows the OS, live). The basemap tint (`utils/basemaps.ts`) follows the same setting.

## Where things live

| File | Purpose |
| --- | --- |
| `src/lib/styles/tokens.css` | **All** design tokens (both themes) and the legacy aliases |
| `src/lib/styles/theme.css` | Reset, scrollbars, form controls, HUD primitives (LED, chip, brackets, grid) |
| `src/lib/styles/fonts.css` + `fonts/` | Bundled IBM Plex Sans (400/500/600) and Plex Mono (400/500), latin subset, OFL licence texts alongside |
| `src/lib/utils/theme.ts` | Sets `data-theme` on `<html>` (and the legacy `.theme-light` / `.theme-auto` classes) |
| `src/lib/components/LogoMark.svelte`, `src/lib/assets/icon.png` | The app icon, shown **unchanged** (a copy of `src-tauri/icons/icon-256.png`); the component only frames it (hairline ring, optional sweep arc) |
| `src/lib/components/RadarSweep.svelte` | The decorative scope for empty / boot states |

Fonts are served from the app itself (no font host, the CSP only allows `'self'`).

## Tokens

Use tokens, never literals. Translucent tints use the `-rgb` triplets:
`background: rgba(var(--c-accent-rgb), 0.12)`.

**Surfaces** (`--c-surface-0..3`): 0 is the canvas behind everything, 1 the panels, 2 raised headers
and cards, 3 hover and popovers. Dark: `#03060c #070c16 #0b1322 #121d33`.

**Lines**: `--c-border` (hairline, cyan at low alpha) and `--c-border-strong`.

**Text**: `--c-text`, `--c-text-dim`, `--c-muted`. All pass WCAG AA (4.5:1) on surfaces 0-3 in both themes
(muted on dark is 5.4-6.6:1, on light 4.5-5.4:1).

**Accents** (restrained, each with one meaning):

| Token | Dark | Meaning |
| --- | --- | --- |
| `--c-accent` | `#2bd9f5` electric cyan | interactive, selected, live |
| `--c-derived` | `#b8a4ff` violet | derived / processed data |
| `--c-caution` | `#ffb92e` amber | caution |
| `--c-alarm` | `#ff6370` red | alarm, error, destructive |
| `--c-nominal` | `#3ee3a4` green | nominal, success, connected |

`--c-on-accent` is the text colour on a solid accent fill (dark text in Mission control, white in
Clean room). **Never hard-code `#fff` on an accent fill.** The user may pick a custom accent in
Settings; it overrides `--c-accent`, `--c-accent-rgb`, `--c-accent-strong` and `--c-on-accent`.

**Type**: `--font-sans` (IBM Plex Sans) for UI, `--font-mono` (IBM Plex Mono) for every number,
coordinate and time. Scale `--fs-2xs..2xl` (10, 11, 12, 13, 15, 18, 26 px). Labels are small caps
tracked with `--ls-label` (`.hud-label`). Numbers use `.mono` / `.hud-value` (tabular figures, so a
changing readout never jitters).

**Space** `--space-1..6` (4 px grid). **Radius** is small (`--radius-xs..xl` = 2, 3, 4, 6, 10 px):
instruments, not bubbles. **Elevation** `--shadow-1..3`; **glow** (`--glow-accent`, `--glow-*`) is
for active and focused elements only.

**Motion** `--dur-fast/base/slow` (120 / 200 / 360 ms), `--ease-out`, `--ease-in-out`.

### Legacy aliases

The old names (`--bg-primary`, `--bg-secondary`, `--text-muted`, `--border-color`,
`--accent-primary`, `--accent-success`, `--glass-bg`, `--spacing-md`, `--radius-sm`,
`--transition-fast` ...) are still defined, as aliases of the tokens, at the bottom of
`tokens.css`. Older components therefore follow the theme with no change. Prefer the new names in
new code; migrate old components opportunistically.

## Components

* **Buttons** (`button`, `button.primary`, `button.danger`): flat, hairline border, cyan fill with
  dark text for the primary action. Sentence case: clarity over decoration.
* **Fields** (`input`, `select`, `textarea`): dark inset, cyan border and a faint glow on focus.
  Numbers are monospace. `select` has a CSS-drawn caret.
* **Checkbox / radio**: square hairline box, cyan when set. **Slider**: hairline track, fader-cap thumb.
* **`.seg`** segmented control and **`.tabs`**: active item gets a cyan underline and tint.
* **`.chip`** (+ `.derived .caution .alarm .nominal`), **`.led`** (+ `.nominal .caution .alarm .accent .derived .off`, `.pulse`, `.blink`), **`kbd`**.
* **Chrome**: `.brackets` (two corner brackets, cards) and `.brackets-4` (all four, dialogs);
  `.seg-rule` (hairline with a cyan tick); `.hud-grid` (faint graticule + scan lines for *empty*
  areas only).
* **Panels** are drawn by `PanelManager`: an index (`01`), a line icon, the tracked label, a cyan
  notch and segmented hairline when open. Panel bodies use `CollapsiblePanel`.
* **Shell**: the top bar carries the app icon, workspace, search, transport, view switch, a UTC mission
  clock and the engine LED; the bottom bar is the system status strip (engine state, file,
  open files, FPS, memory where the webview reports it).
* **Boot / error**: `EngineBoot.svelte` shows `INITIALISING ENGINE` with real status (shell, engine
  address / port, handshake attempt, elapsed time, errors raised by the Tauri shell) and turns red
  (`ENGINE FAULT`) with a Retry button when the engine cannot be reached.

## Motion

Short and purposeful: panel open/close, toasts sliding in, the radar sweep on the welcome, boot
and empty states, the logo sweep while connecting, `.flash` for a value change. Nothing animates over
the data canvas. Under `prefers-reduced-motion` every animation and transition collapses to an
instant state change (global rule in `theme.css`) and the sweep is drawn frozen.

## Accessibility

* Text contrast is AA or better in both themes (see the table in *Tokens*).
* Focus is always visible: 2 px accent outline (text fields use the accent border + glow).
* State is never colour alone: LEDs sit next to a word (`NOMINAL`, `OFFLINE`), toasts carry an icon.
* Targets stay >= 26 px high; the sweep and grid are `aria-hidden`.

## Adding a new panel

1. Create `MyPanel.svelte`. Wrap its content in `<CollapsiblePanel title="..." badge={...}>`
   (inside the right sidebar the header is drawn for you; you only provide the body).
2. Register it in `PANEL_MAP` in `PanelManager.svelte` (`component`, `label`, `icon`). Optionally add
   a line icon to `PANEL_ICONS` (same file); without one the `icon` text is shown.
3. Style with tokens only: `var(--c-surface-*)`, `var(--c-border)`, `var(--c-accent)`,
   `var(--font-mono)` for numbers, `.hud-label` for field labels. Use the shared `button`, `select`,
   `input`, `.seg`, `.chip` styles instead of redefining them.
4. Check it in both themes (Settings -> Appearance) and at 1100 px wide.

## Do and don't

* Do not redraw or recolour the app icon. Frame it with CSS only.
* Do keep glow for the active or focused element; do not glow whole panels.
* Do use violet only for *derived* data (module / retrieval output), amber for caution, red for alarm.
* Do not put the grid, scan lines or any animation behind data.
* Do not add a colour literal; add a token (in both themes) if you need a new one.
