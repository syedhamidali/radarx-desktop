/**
 * Layout of the PERiLS movie's overlay: which text and cards are drawn where, and what gives way to what.
 *
 * Two kinds of item are drawn over the 3-D scene:
 *  - cards (the heads-up display: insets, captions, the timeline, ...), each at a fixed place of its own, and
 *  - labels (names of radars and stations, "vortex candidate", the track's start, ...), each tied to a point of the scene.
 *
 * Rules (the film is checked against them frame by frame, `findViolations`):
 *  1. nothing leaves the frame (a card lies inside it; a label is moved inside, or dropped);
 *  2. two items that are both visible (alpha >= VISIBLE) never overlap: where two overlap, the one of lower priority
 *     yields, i.e. its alpha is cut by the alpha of the other (`yieldAlpha`: it is gone as soon as the other reaches a
 *     quarter of its opacity), so that cross-fades do not leave two texts on top of each other;
 *  3. a label is tried at several places around its anchor (`DEFAULT_OFFSETS`), the place it had before first; when none is
 *     free it is dropped (it fades out); at most `maxLabels` labels are shown, the most important first;
 *  4. labels fade in and out at a fixed rate (`FADE_S`), and a label that is shown has a head start over a new one of the
 *     same importance, so that a label does not flicker when two of them compete for the same place.
 *
 * Pure functions and classes without any drawing: the canvas code asks for places and draws what it is told to.
 */

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** An item counts as visible (and is checked for overlap) at or above this alpha. */
export const VISIBLE = 0.2;
/** Seconds a label takes to fade in or out. */
export const FADE_S = 0.3;
/** A label that is not on screen must have had a free place for this many frames in a row before it starts to fade in (no flashes). */
export const ENTER_FRAMES = 8;
/** Head start (in priority points) of a label that is on screen over a new one. */
export const INCUMBENT_BONUS = 14;
/** Gap (in `u`) kept between two labels, and between a label and a card. */
export const LABEL_GAP = 3;
export const CARD_GAP = 5;
/** Alpha of the lower-priority item is multiplied by 1 - YIELD * (alpha of the higher one). */
export const YIELD = 4;

export const rectRight = (r: Rect): number => r.x + r.w;
export const rectBottom = (r: Rect): number => r.y + r.h;

/** True when the rectangles overlap by more than `pad` px of margin on every side (pad < 0 shrinks them). */
export function rectsOverlap(a: Rect, b: Rect, pad = 0): boolean {
  return a.x < b.x + b.w + pad && b.x < a.x + a.w + pad && a.y < b.y + b.h + pad && b.y < a.y + a.h + pad;
}

/** True when `r` lies inside the frame `W` x `H` with `margin` px to spare (a 0.5 px tolerance for rounding). */
export function rectInside(r: Rect, W: number, H: number, margin = 0): boolean {
  const e = 0.5;
  return r.x >= margin - e && r.y >= margin - e && r.x + r.w <= W - margin + e && r.y + r.h <= H - margin + e;
}

const clamp01 = (v: number): number => Math.max(0, Math.min(1, v));

/** The alpha of an item after the higher-priority items (their alphas) that it overlaps: gone when one of them reaches 1/YIELD. */
export function yieldAlpha(alpha: number, higher: number[]): number {
  let a = alpha;
  for (const h of higher) a *= clamp01(1 - YIELD * h);
  return a;
}

export interface Item {
  id: string;
  /** The pieces of one card (its lines, say) share a group and do not count against each other. */
  group?: string;
  kind: 'card' | 'label';
  rect: Rect;
  /** The alpha the item is drawn with (after yielding). */
  alpha: number;
}

/** What one frame drew: the cards and the labels with their rectangles. */
export class FrameLayout {
  readonly items: Item[] = [];
  readonly W: number;
  readonly H: number;
  readonly u: number;

  // (no constructor parameter properties: the unit tests run the file with Node's type stripping)
  constructor(W: number, H: number, u: number) {
    this.W = W;
    this.H = H;
    this.u = u;
  }

  /**
   * Register a card with its rectangle and the alpha it wants; returns the alpha to draw it with, which is cut by the
   * visible cards registered before it that it overlaps (register the more important cards first).
   */
  card(id: string, rect: Rect | Rect[], alpha: number): number {
    const rects = Array.isArray(rect) ? rect : [rect];
    const higher = this.items.filter((i) => i.kind === 'card' && i.alpha > 0 && rects.some((r) => rectsOverlap(i.rect, r))).map((i) => i.alpha);
    const a = yieldAlpha(alpha, higher);
    rects.forEach((r, k) => this.items.push({ id: rects.length > 1 ? `${id}#${k}` : id, group: rects.length > 1 ? id : undefined, kind: 'card', rect: r, alpha: a }));
    return a;
  }

  /** The cards a label must keep clear of (visible ones). */
  obstacles(): Rect[] {
    return this.items.filter((i) => i.kind === 'card' && i.alpha >= VISIBLE).map((i) => i.rect);
  }

  /** Whether `rect` is free of every visible card, with a margin. */
  isFree(rect: Rect, gap = CARD_GAP * this.u): boolean {
    return this.obstacles().every((o) => !rectsOverlap(o, rect, gap));
  }
}

/** A place for a card: which column, the y of its top, and the lowest y it may reach. */
export interface Place {
  side: string;
  y: number;
  bottom: number;
}

/** The first of the places where a card of height `h` fits entirely (its bottom at or above the place's limit), or null. */
export function firstFit(h: number, places: Place[]): Place | null {
  return places.find((p) => p.y + h <= p.bottom + 0.5) ?? null;
}

/**
 * Overlaps and exits of one frame: pairs of items that are both visible and overlap, and items (cards, labels) that are not
 * inside the frame. `margin` is the safe area in px. Empty when the frame obeys the rules.
 */
export function findViolations(items: Item[], W: number, H: number, margin = 0): string[] {
  const out: string[] = [];
  const vis = items.filter((i) => i.alpha > VISIBLE);
  for (const i of vis) if (!rectInside(i.rect, W, H, margin)) out.push(`${i.id} leaves the frame`);
  for (let a = 0; a < vis.length; a++) {
    for (let b = a + 1; b < vis.length; b++) {
      if (vis[a].group && vis[a].group === vis[b].group) continue;
      if (rectsOverlap(vis[a].rect, vis[b].rect)) out.push(`${vis[a].id} overlaps ${vis[b].id}`);
    }
  }
  return out;
}

// ─── labels ──────────────────────────────────────────────────────────────────

export interface LabelReq {
  id: string;
  text: string;
  /** The scene point the label belongs to, px. */
  ax: number;
  ay: number;
  /** Font size at 1080 p and weight (the width is measured at the frame's `u`). */
  size: number;
  weight?: number;
  colour: string;
  /** Importance: higher is kept, lower is dropped first. */
  priority: number;
  /** Opacity of the label's layer in this shot (0..1). */
  alpha: number;
  /** Places to try, as the offset (in `u`) of the text's left baseline from the anchor (text is left-aligned); a function gets the text width in `u`. */
  offsets?: [number, number][] | ((wu: number) => [number, number][]);
  /** Where the text may go at most this far (in `u`) from the first offset by being moved back inside the frame. */
  maxShift?: number;
  /** true: when no place near the anchor is free, slide the label up (130 u at most) until it is clear and tie it to its anchor; else it is dropped. */
  slide?: boolean;
}

/** Right of the anchor and above, below, then left, above and below it. `w` is the text width in `u`. */
export function defaultOffsets(w: number, size: number): [number, number][] {
  return [
    [10, -6],
    [10, size + 8],
    [-(w + 10), -6],
    [-(w + 10), size + 8],
    [-w / 2, -(size + 10)],
    [-w / 2, size + 16],
  ];
}

export interface DrawnLabel {
  id: string;
  text: string;
  /** Left baseline of the text, px. */
  x: number;
  y: number;
  size: number;
  weight: number;
  colour: string;
  alpha: number;
  rect: Rect;
  /** A thin line from the anchor to the text, for a label that had to move away from its anchor. */
  leader: [number, number, number, number] | null;
}

interface LabelState {
  s: number;
  cand: number;
  rect: Rect;
  req: LabelReq;
  x: number;
  y: number;
  shown: boolean;
  leader: [number, number, number, number] | null;
}

/** Width in px of `text` at the given font size (px) and weight. */
export type Measure = (text: string, sizePx: number, weight: number) => number;

/**
 * Places the labels of a frame and keeps their fades between frames. One instance for the whole film; `place` is called once
 * per frame with the frame's requests, after the cards have been registered in the `FrameLayout`.
 */
export class LabelPlacer {
  private state = new Map<string, LabelState>();
  /** Frames in a row for which a label that is not shown has had a free place. */
  private waiting = new Map<string, number>();
  private lastT = Number.NaN;
  private measureFn: Measure;
  readonly maxLabels: number;

  constructor(measureFn: Measure, maxLabels = 8) {
    this.measureFn = measureFn;
    this.maxLabels = maxLabels;
  }

  /** The text box of a label whose left baseline is at (x, y): ascent 0.92 em, descent 0.28 em, a pixel or two of halo. */
  static box(x: number, y: number, w: number, sizePx: number): Rect {
    return { x: x - 1.5, y: y - sizePx * 0.92 - 1.5, w: w + 3, h: sizePx * 1.2 + 3 };
  }

  /** Forget the fades (a new film, or a jump in time). */
  reset(): void {
    this.state.clear();
    this.waiting.clear();
    this.lastT = Number.NaN;
  }

  place(reqs: LabelReq[], layout: FrameLayout, tc: number): DrawnLabel[] {
    const { W, H, u } = layout;
    const dt = tc - this.lastT;
    // frames come in order at 1/30 s; a jump (or the first frame) takes the targets at once
    const jump = !(dt > 0 && dt <= 0.5);
    this.lastT = tc;
    const step = jump ? 1 : dt / FADE_S;
    const byId = new Map<string, LabelReq>();
    for (const r of reqs) if (r.alpha > 0.02 && !byId.has(r.id)) byId.set(r.id, r);
    // importance, with a head start for a label that is on screen
    const score = (r: LabelReq): number => r.priority + ((this.state.get(r.id)?.s ?? 0) > 0.5 ? INCUMBENT_BONUS : 0);
    // the labels that are on screen (or fading out) are placed first and keep their rectangle, shown or not, until they are gone:
    // a new label never takes the place of one that is still visible, it waits (so labels do not push each other out and in)
    const byScore = (a: LabelReq, b: LabelReq): number => score(b) - score(a) || (a.id < b.id ? -1 : 1);
    const all = [...byId.values()];
    const order = [...all.filter((r) => (this.state.get(r.id)?.s ?? 0) > 0).sort(byScore), ...all.filter((r) => !((this.state.get(r.id)?.s ?? 0) > 0)).sort(byScore)];
    const placed: Rect[] = [];
    for (const [id, st] of this.state) if (!byId.has(id) && st.s > 0) placed.push(st.rect);
    const keep = new Map<string, LabelState>();
    const waiting = new Map<string, number>();
    const margin = 6 * u;
    for (const r of order) {
      const sizePx = r.size * u;
      const weight = r.weight ?? 600;
      const wPx = this.measureFn(r.text, Math.round(sizePx), weight);
      const prev = this.state.get(r.id);
      const offs = typeof r.offsets === 'function' ? r.offsets(wPx / u) : (r.offsets ?? defaultOffsets(wPx / u, r.size));
      const idx = [...offs.keys()];
      if (prev && prev.cand >= 0 && prev.cand < offs.length) idx.sort((a, b) => (a === prev.cand ? -1 : b === prev.cand ? 1 : a - b));
      let found: { x: number; y: number; rect: Rect; cand: number; leader: [number, number, number, number] | null } | null = null;
      const clear = (rect: Rect): boolean => layout.isFree(rect, CARD_GAP * u) && !placed.some((p) => rectsOverlap(p, rect, LABEL_GAP * u));
      if (placed.length < this.maxLabels) {
        for (const k of idx) {
          let x = r.ax + offs[k][0] * u;
          let y = r.ay + offs[k][1] * u;
          // moved back inside the frame, by 60 u at most (a label whose anchor is outside is not drawn)
          const lim = (r.maxShift ?? 60) * u;
          const cx = Math.max(margin, Math.min(W - margin - wPx, x));
          const cy = Math.max(margin + sizePx * 0.92, Math.min(H - margin - sizePx * 0.28, y));
          if (Math.abs(cx - x) > lim || Math.abs(cy - y) > lim) continue;
          x = cx;
          y = cy;
          const rect = LabelPlacer.box(x, y, wPx, sizePx);
          if (!clear(rect)) continue;
          found = { x, y, rect, cand: k, leader: null };
          break;
        }
        // none free near the anchor (it is under a card, say): slide up from the first place, up to 130 u, and tie it to its anchor
        if (!found && r.slide) {
          const x0 = Math.max(margin, Math.min(W - margin - wPx, r.ax + offs[0][0] * u));
          for (let up = 12; up <= 130 && !found && r.ay < H + 20 * u; up += 6) {
            const y = Math.max(margin + sizePx * 0.92, Math.min(H - margin - sizePx * 0.28, r.ay + offs[0][1] * u - up * u));
            const rect = LabelPlacer.box(x0, y, wPx, sizePx);
            if (clear(rect)) found = { x: x0, y, rect, cand: 0, leader: [r.ax, r.ay, Math.max(rect.x, Math.min(r.ax, rect.x + rect.w)), rect.y + rect.h] };
          }
        }
      }
      if (found && !jump && !(prev && prev.s > 0)) {
        // a new label: it must have had its place for ENTER_FRAMES frames before it appears (its place is kept free meanwhile)
        const n = (this.waiting.get(r.id) ?? 0) + 1;
        waiting.set(r.id, n);
        placed.push(found.rect);
        if (n < ENTER_FRAMES) continue;
      } else if (found) {
        placed.push(found.rect);
      }
      if (found) {
        const s0 = prev?.s ?? 0;
        keep.set(r.id, { s: jump ? 1 : Math.min(1, s0 + step), cand: found.cand, rect: found.rect, req: r, x: found.x, y: found.y, shown: true, leader: found.leader });
      } else if (prev && prev.s > 0) {
        // not placed: it fades out where it was (its rectangle still counts for the labels of lower priority)
        const s = jump ? 0 : Math.max(0, prev.s - step);
        if (s > 0) {
          keep.set(r.id, { ...prev, s, req: r, shown: false });
          placed.push(prev.rect);
        }
      }
    }
    // labels with no request any more (the layer ended, the anchor left the screen) fade out where they were
    for (const [id, st] of this.state) {
      if (keep.has(id) || byId.has(id)) continue;
      const s = jump ? 0 : Math.max(0, st.s - step);
      if (s > 0) keep.set(id, { ...st, s, shown: false });
    }
    this.state = keep;
    this.waiting = waiting;
    // alphas: layer x fade, cut by the visible cards and by the visible labels of higher importance that it overlaps
    const out: DrawnLabel[] = [];
    const ranked = [...keep.entries()].sort((a, b) => score(b[1].req) - score(a[1].req) || (a[0] < b[0] ? -1 : 1));
    const done: { rect: Rect; alpha: number }[] = [];
    const cards = layout.items.filter((i) => i.kind === 'card' && i.alpha > 0);
    for (const [id, st] of ranked) {
      const higher = [...cards.filter((c) => rectsOverlap(c.rect, st.rect)).map((c) => c.alpha), ...done.filter((d) => rectsOverlap(d.rect, st.rect)).map((d) => d.alpha)];
      const alpha = yieldAlpha(st.req.alpha * st.s, higher);
      done.push({ rect: st.rect, alpha });
      if (alpha <= 0.01) continue;
      const sizePx = st.req.size * u;
      out.push({ id, text: st.req.text, x: st.x, y: st.y, size: st.req.size, weight: st.req.weight ?? 600, colour: st.req.colour, alpha, rect: st.rect, leader: st.leader });
      layout.items.push({ id: `label:${id}`, kind: 'label', rect: st.rect, alpha });
      void sizePx;
    }
    return out;
  }
}
