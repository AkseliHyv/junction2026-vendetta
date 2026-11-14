/* Noise field on a 10 m grid over the mapped area.
 *
 * 1. A propagation model spreads each OSM street over the grid as a line
 *    source: about 3 dB less per doubling of distance, plus extra attenuation
 *    for ground and buildings. Energy is kept per source kind so the field for
 *    any hour is a cheap weighted sum (kinds follow different daily profiles).
 * 2. Station measurements correct the model: each station's residual
 *    (measured LAeq − modelled level at the station) is spread with a Gaussian
 *    kernel and added in dB, fading back to the pure model far from stations.
 * 3. The result is colour-mapped into an offscreen RGBA canvas (quiet areas
 *    transparent) that the map lays over the basemap.
 */
window.NM = window.NM || {};

NM.field = (function () {
  const U = NM.util, D = NM.data, W = D.world;

  const CELL = 10; // metres per grid cell
  const nx = Math.ceil((W.x1 - W.x0) / CELL) + 1;
  const ny = Math.ceil((W.y1 - W.y0) / CELL) + 1;
  const N = nx * ny;

  const KINDS = ['arterial', 'street', 'lane', 'ambient'];
  const layers = {};
  for (const k of KINDS) layers[k] = new Float32Array(N);

  const BG_E = U.dbToE(38); // residual city hum
  const REACH = { arterial: 650, street: 380, lane: 170, ambient: 90 }; // metres a source is computed out to
  const D0 = 8; // metres: half the carriageway, where the line-source falloff starts
  const EXCESS = 230; // metres: e-folding length of ground and building attenuation
  const RESIDUAL_RADIUS = 320; // metres
  const RESIDUAL_PRIOR = 0.12; // pulls the correction to 0 away from stations
  const EDGE_FADE = 250; // metres: the overlay fades out at the edge of the area

  const dbRaw = new Float32Array(N);
  const dbSmooth = new Float32Array(N);
  const tmp = new Float32Array(N);
  const edge = new Float32Array(N);

  const canvas = document.createElement('canvas');
  canvas.width = nx;
  canvas.height = ny;
  const ctx = canvas.getContext('2d');
  const image = ctx.createImageData(nx, ny);

  // ---- Colour map (dB → rgba) ------------------------------------------------
  // The alpha lets the basemap show through quiet areas.
  const STOPS = [
    [40, '#1a1530', 0],
    [45, '#211a3c', 0.18],
    [50, '#2c2350', 0.4],
    [54, '#45306e', 0.56],
    [58, '#5e3580', 0.66],
    [62, '#84407f', 0.74],
    [65, '#a84879', 0.8],
    [68, '#cc576f', 0.84],
    [72, '#e57a70', 0.88],
    [76, '#fbb08a', 0.92],
    [82, '#fff1cc', 0.95],
  ].map(([db, hex, a]) => [db, [...U.hexToRgb(hex), a * 255]]);
  const LUT_MIN = 30, LUT_STEP = 0.25, LUT_N = 241;
  const LUT = new Uint8ClampedArray(LUT_N * 4);
  for (let i = 0; i < LUT_N; i++) LUT.set(colorFromStops(LUT_MIN + i * LUT_STEP), i * 4);
  function colorFromStops(db) {
    if (db <= STOPS[0][0]) return STOPS[0][1];
    for (let i = 1; i < STOPS.length; i++) {
      if (db <= STOPS[i][0]) {
        const [d0, c0] = STOPS[i - 1], [d1, c1] = STOPS[i], t = (db - d0) / (d1 - d0);
        return c0.map((v, k) => v + (c1[k] - v) * t);
      }
    }
    return STOPS[STOPS.length - 1][1];
  }
  /** Opaque colour for a level (used for street glow and markers). */
  function color(db) {
    const i = U.clamp(Math.round((db - LUT_MIN) / LUT_STEP), 0, LUT_N - 1) * 4;
    return [LUT[i], LUT[i + 1], LUT[i + 2]];
  }

  // ---- Daily profiles ------------------------------------------------------
  function profileDb(kind, hf) {
    const p = D.profiles[kind];
    const h0 = Math.floor(hf) % 24, h1 = (h0 + 1) % 24;
    return U.lerp(p[h0], p[h1], hf - Math.floor(hf));
  }
  function gains(hf) {
    const g = {};
    for (const k of KINDS) g[k] = U.dbToE(profileDb(k, hf));
    return g;
  }

  // ---- Build static layers ---------------------------------------------------
  // Falloff with distance, tabulated per kind in 0.5 m steps. The window term
  // takes it smoothly to 0 at the reach so there is no visible edge.
  const FALLOFF = {};
  for (const k of KINDS) {
    const R = REACH[k], n = Math.ceil(R * 2) + 1, lut = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const d = i / 2, win = Math.max(0, 1 - (d / R) ** 2);
      lut[i] = (D0 / (D0 + d)) * Math.exp(-d / EXCESS) * win * win;
    }
    FALLOFF[k] = lut;
  }

  function build() {
    const t0 = performance.now();
    for (const k of KINDS) layers[k].fill(0);

    // Within a kind, each cell keeps the loudest street (max, not sum), so the
    // many short OSM ways a street is split into don't add up at their joints.
    for (const road of D.roads) {
      const E = U.dbToE(road.level), dst = layers[road.kind], lut = FALLOFF[road.kind];
      const R = REACH[road.kind], R2 = R * R, pts = road.pts;
      for (let s = 0; s < pts.length - 1; s++) {
        const ax = pts[s][0], ay = pts[s][1], bx = pts[s + 1][0], by = pts[s + 1][1];
        const i0 = Math.max(0, Math.floor((Math.min(ax, bx) - R - W.x0) / CELL));
        const i1 = Math.min(nx - 1, Math.ceil((Math.max(ax, bx) + R - W.x0) / CELL));
        const j0 = Math.max(0, Math.floor((Math.min(ay, by) - R - W.y0) / CELL));
        const j1 = Math.min(ny - 1, Math.ceil((Math.max(ay, by) + R - W.y0) / CELL));
        if (i0 > i1 || j0 > j1) continue;
        const dx = bx - ax, dy = by - ay, len2 = dx * dx + dy * dy || 1e-9;
        for (let j = j0; j <= j1; j++) {
          const y = W.y0 + j * CELL, row = j * nx;
          for (let i = i0; i <= i1; i++) {
            const x = W.x0 + i * CELL;
            let t = ((x - ax) * dx + (y - ay) * dy) / len2;
            t = t < 0 ? 0 : t > 1 ? 1 : t;
            const ex = ax + dx * t - x, ey = ay + dy * t - y, d2 = ex * ex + ey * ey;
            if (d2 >= R2) continue;
            const v = E * lut[(Math.sqrt(d2) * 2) | 0];
            if (v > dst[row + i]) dst[row + i] = v;
          }
        }
      }
    }

    for (let j = 0; j < ny; j++) {
      for (let i = 0; i < nx; i++) {
        const d = Math.min(i, j, nx - 1 - i, ny - 1 - j) * CELL;
        const t = U.clamp(d / EDGE_FADE, 0, 1);
        edge[j * nx + i] = t * t * (3 - 2 * t);
      }
    }
    console.info(`[noise-map] noise model built: ${D.roads.length} streets, ${nx}×${ny} cells, ${Math.round(performance.now() - t0)} ms`);
  }

  /** Separable box blur, in place. */
  function blur(a, r, passes) {
    for (let p = 0; p < passes; p++) {
      for (let j = 0; j < ny; j++) boxLine(a, tmp, j * nx, 1, nx, r);
      for (let i = 0; i < nx; i++) boxLine(tmp, a, i, nx, ny, r);
    }
  }
  function boxLine(src, dst, off, stride, n, r) {
    let acc = 0;
    const at = (k) => src[off + U.clamp(k, 0, n - 1) * stride];
    for (let k = -r; k <= r; k++) acc += at(k);
    const inv = 1 / (2 * r + 1);
    for (let k = 0; k < n; k++) {
      dst[off + k * stride] = acc * inv;
      acc += at(k + r + 1) - at(k - r);
    }
  }

  // ---- Sampling --------------------------------------------------------------
  function bilinear(arr, x, y) {
    const fx = U.clamp((x - W.x0) / CELL, 0, nx - 1.001), fy = U.clamp((y - W.y0) / CELL, 0, ny - 1.001);
    const i = Math.floor(fx), j = Math.floor(fy), tx = fx - i, ty = fy - j;
    const k = j * nx + i;
    return (arr[k] * (1 - tx) + arr[k + 1] * tx) * (1 - ty) + (arr[k + nx] * (1 - tx) + arr[k + nx + 1] * tx) * ty;
  }

  /** Pure model level (no station correction) at a point and hour of day. */
  function modelAt(x, y, hf) {
    const g = gains(hf);
    let E = BG_E;
    for (const k of KINDS) E += g[k] * bilinear(layers[k], x, y);
    return U.eToDb(E);
  }

  function residualAt(x, y, res) {
    let num = 0, den = RESIDUAL_PRIOR;
    const r2 = RESIDUAL_RADIUS * RESIDUAL_RADIUS;
    for (const s of res) {
      const w = Math.exp(-((x - s.x) ** 2 + (y - s.y) ** 2) / r2);
      num += w * s.r;
      den += w;
    }
    return num / den;
  }

  // ---- Per-update state -------------------------------------------------------
  let current = { hf: 12, residuals: [] };

  /**
   * Recompute the field for time `t` (ms), corrected by stations:
   * [{ x, y, laeq }] (their latest 15-minute LAeq).
   */
  function update(stations, t) {
    const hf = NM.time.hourFrac(t);
    const g = gains(hf);
    const residuals = stations.map((s) => ({ x: s.x, y: s.y, r: s.laeq - modelAt(s.x, s.y, hf) }));
    current = { hf, residuals, t };

    const A = layers.arterial, S = layers.street, L = layers.lane, M = layers.ambient;
    const r2 = RESIDUAL_RADIUS * RESIDUAL_RADIUS;
    // The Gaussian weights are separable: precompute them per column and row.
    const wx = residuals.map((s) => Float32Array.from({ length: nx }, (_, i) => Math.exp(-((W.x0 + i * CELL - s.x) ** 2) / r2)));
    const wy = residuals.map((s) => Float32Array.from({ length: ny }, (_, j) => Math.exp(-((W.y0 + j * CELL - s.y) ** 2) / r2)));
    for (let j = 0; j < ny; j++) {
      for (let i = 0; i < nx; i++) {
        const k = j * nx + i;
        const E = BG_E + g.arterial * A[k] + g.street * S[k] + g.lane * L[k] + g.ambient * M[k];
        let num = 0, den = RESIDUAL_PRIOR;
        for (let s = 0; s < residuals.length; s++) {
          const w = wx[s][i] * wy[s][j];
          num += w * residuals[s].r;
          den += w;
        }
        dbRaw[k] = U.eToDb(E) + num / den;
      }
    }

    dbSmooth.set(dbRaw);
    blur(dbSmooth, 1, 2);
    paint();
  }

  function paint() {
    const px = image.data;
    for (let k = 0; k < N; k++) {
      const li = U.clamp(Math.round((dbSmooth[k] - LUT_MIN) / LUT_STEP), 0, LUT_N - 1) * 4;
      const o = k * 4;
      px[o] = LUT[li]; px[o + 1] = LUT[li + 1]; px[o + 2] = LUT[li + 2];
      px[o + 3] = LUT[li + 3] * edge[k];
    }
    ctx.putImageData(image, 0, 0);
  }

  /** Current (corrected) level at a point. */
  function dbAt(x, y) {
    return bilinear(dbRaw, x, y);
  }

  /**
   * Hourly levels at a point for clock hours 0..23. `stationsHourly` is
   * [{ x, y, hours: [laeq|null × 24] }]. Each hour uses that hour's model,
   * corrected by the stations that reported during that hour.
   */
  function hourlyAt(x, y, stationsHourly) {
    const out = new Array(24);
    for (let h = 0; h < 24; h++) {
      const hf = h + 0.5;
      const res = [];
      for (const s of stationsHourly) {
        const m = s.hours[h];
        if (m != null) res.push({ x: s.x, y: s.y, r: m - modelAt(s.x, s.y, hf) });
      }
      out[h] = modelAt(x, y, hf) + residualAt(x, y, res);
    }
    return out;
  }

  /** Corners of the field image as [lng, lat]: top-left, top-right, bottom-right, bottom-left. */
  function corners() {
    const x0 = W.x0 - CELL / 2, y0 = W.y0 - CELL / 2, x1 = x0 + nx * CELL, y1 = y0 + ny * CELL;
    return [NM.geo.toLngLat(x0, y0), NM.geo.toLngLat(x1, y0), NM.geo.toLngLat(x1, y1), NM.geo.toLngLat(x0, y1)];
  }

  return {
    build, update, dbAt, modelAt, hourlyAt, color, corners, canvas,
    get current() { return current; },
  };
})();
