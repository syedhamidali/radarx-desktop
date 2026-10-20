/**
 * The simulated tornado of the tornado shot (src/lib/utils/tornado/TornadoScene.ts, docs/TORNADO_SIM.md) inside the film.
 *
 * The scene is a physically based SIMULATION seeded by observations: the survey gives where, when, how wide and how
 * strong the tornado was rated, the radar gives the parent circulation, the stations give the air, the LMA the
 * flashes; the swirl, the funnel (from the pressure deficit of the vortex), the dust, the debris and the trees are
 * modelled. It renders on its own WebGL canvas; the film draws that canvas over the radar-echo render with an opacity
 * (`simWeight`) that is zero away from the tornado, so the shot dissolves into the simulation as the camera arrives
 * and out of it as it leaves. The simulation runs at one data second per clip second in the shot (the timeline's
 * core), which is physically right for its swirl.
 */
import type { TornadoScene } from '../utils/tornado/TornadoScene';
import type { SeedData } from '../utils/tornado/seed';
import { vortexOnlyRadius } from '../utils/tornado/funnel.ts';
import { tangential, omega } from '../utils/tornado/vortexField.ts';

export interface SimSeedFile {
  seed: SeedData & { epochUtc?: string };
  notes?: Record<string, unknown>;
}

export interface SimGeoPose {
  lat: number;
  lon: number;
  altM: number;
  headingDeg: number;
  pitchDeg: number;
  fovDeg: number;
}

export interface SimNumbers {
  ef: string;
  vt: number;
  coreRadius: number;
  width: number;
  periodCore: number;
  periodFunnel: number | null;
  ground: string;
  attribution: string[];
  lcl: number;
  /** The visible funnel wall at some heights: radius, tangential wind and angular velocity of the model (for the check on screen). */
  wall: { z: number; r: number; vt: number; omega: number; period: number }[];
}

const smooth = (x: number): number => {
  const c = Math.max(0, Math.min(1, x));
  return c * c * (3 - 2 * c);
};

/**
 * Opacity of the simulation from the camera's distance to the tornado (km): full inside `near`, none beyond `far`,
 * smooth between. Far away the simulation has nothing to show (its ground ends at about 3 km) and the radar echo is the
 * picture; close, the simulation is. Pure; unit-tested.
 */
export function simDistanceWeight(distKm: number, near = 1.8, far = 4.5): number {
  return 1 - smooth((distKm - near) / (far - near));
}

/**
 * The simulation runs near real time only (its swirl is physical at one data second per clip second): its opacity goes to
 * zero as the data clock speeds up, from x6 (full) to x30 (none): the time-lapse stretches between the real-time angles show the simulation at the speed of the data clock (a fast hyperlapse of it) up to x6 and fade it into the radar-echo scene beyond.
 */
/**
 * Extinction of the simulation's rain relative to the physical value, from the camera's distance to the tornado (km): 1 inside 0.55 km,
 * 0.12 beyond 1.2 km. From far, 50 dBZ of rain would hide the whole funnel; the film draws the rain thinner there so that the funnel can
 * be seen (a display choice, said in docs/PERILS_MOVIE.md); the pattern of the rain is the radar's either way.
 */
export function simRainGain(distKm: number, near = 0.55, far = 1.2, floor = 0.12): number {
  return 1 - (1 - floor) * smooth((distKm - near) / (far - near));
}

export function simSpeedWeight(speedup: number, from = 6, to = 30): number {
  return 1 - smooth((speedup - from) / (to - from));
}

export class SimTornado {
  scene: TornadoScene | null = null;
  ready = false;
  numbers: SimNumbers | null = null;
  /** Why the simulation is not drawn (no WebGL2, no seed...), for the report. */
  error: string | null = null;
  private shot: [number, number] = [0, 0];

  /**
   * Build the scene: the seed, the shot span (data seconds), the ground (imagery from the tile cache when there is one,
   * else the procedural farmland) and the cleared places around the camera path.
   */
  async init(
    file: SimSeedFile,
    shot: [number, number],
    size: { width: number; height: number; scale?: number; land?: { half: number; size: number; treeRadius: number } },
    path: { tc: number; td: number; geo: SimGeoPose }[],
  ): Promise<void> {
    try {
      const { TornadoScene: Scene } = await import('../utils/tornado/TornadoScene');
      const canvas = document.createElement('canvas');
      const scene = new Scene({
        canvas, width: size.width, height: size.height, ambient: 'dusk', useTiles: true, tileBase: '', renderScale: size.scale,
        landHalf: size.land?.half, landSize: size.land?.size, treeRadius: size.land?.treeRadius,
      });
      scene.setSeedData(file.seed);
      scene.setShot(shot[0], shot[1], 3500);
      await scene.loadGround();
      // keep trees off the places the camera stands in: a wide berth where it is far from the vortex (the chaser sees the
      // funnel past them), a tight one where it is close (so that the trees stay to be thrown about)
      const far: [number, number][] = [];
      const near: [number, number][] = [];
      for (const p of path) {
        const [x, y] = scene.toLocal(p.geo.lat, p.geo.lon);
        const a = scene.axis(p.td);
        (Math.hypot(x - a.x, y - a.y) > 300 ? far : near).push([x, y]);
      }
      scene.clearPositions(far, 160);
      scene.clearPositions(near, 40);
      const i = scene.info(shot[0] + 0.5 * (shot[1] - shot[0]));
      this.numbers = {
        ef: i.ef, vt: i.vt, coreRadius: i.coreRadius, width: i.width, periodCore: i.periodCore, periodFunnel: i.periodFunnel,
        ground: i.ground, attribution: scene.groundInfo.attribution, lcl: i.lcl,
        wall: [20, 40, 60, 80, 100, 130, 160].map((z) => {
          const sd = scene.seed!;
          const r = vortexOnlyRadius(sd.condition, sd.vortex, z);
          const w = r > 0 ? Math.abs(omega(sd.vortex, r, z)) : 0;
          return { z, r, vt: r > 0 ? tangential(sd.vortex, r, z) : 0, omega: w, period: w > 0 ? (2 * Math.PI) / w : 0 };
        }),
      };
      this.scene = scene;
      this.shot = shot;
      this.ready = true;
    } catch (e) {
      this.error = e instanceof Error ? e.message : String(e);
      this.ready = false;
    }
  }

  /** Is there a tornado to draw at data time `td`? (the survey span; the simulation draws nothing otherwise) */
  active(td: number): boolean {
    return !!this.scene && this.scene.isActive(td);
  }

  /** Axis of the vortex (local metres) at data time `td`. */
  axis(td: number): { x: number; y: number } | null {
    return this.scene ? this.scene.axis(td) : null;
  }

  /** Render the frame at data time `td` for a geographic pose; returns the simulation's canvas (its size is the render size). */
  render(td: number, pose: SimGeoPose, prev?: { td: number; pose: SimGeoPose }, rainGain = 1): HTMLCanvasElement | OffscreenCanvas | null {
    const s = this.scene;
    if (!s || !this.ready) return null;
    s.rainGain = rainGain;
    s.setTime(td);
    const cam = s.cameraFromGeo({ lat: pose.lat, lon: pose.lon, altM: pose.altM, headingDeg: pose.headingDeg, pitchDeg: pose.pitchDeg, fovDeg: pose.fovDeg });
    if (prev && td > prev.td) {
      // the camera's velocity (m/s) for the motion blur of the rain streaks
      const p = s.cameraFromGeo({ lat: prev.pose.lat, lon: prev.pose.lon, altM: prev.pose.altM, headingDeg: prev.pose.headingDeg, pitchDeg: prev.pose.pitchDeg, fovDeg: prev.pose.fovDeg });
      const dt = td - prev.td;
      cam.vel = [(cam.pos[0] - p.pos[0]) / dt, (cam.pos[1] - p.pos[1]) / dt, (cam.pos[2] - p.pos[2]) / dt];
    }
    s.render(cam);
    // the camera the simulation drew (local metres), for the measurement of the rotation in the frames
    const w = window as unknown as Record<string, any>;
    w.__xrPerilsCam = { ...(w.__xrPerilsCam ?? {}), sim: { pos: [...cam.pos], target: [...cam.target], fovDeg: cam.fovDeg } };
    return s.canvas;
  }

  get span(): [number, number] {
    return this.shot;
  }
}
