/* Experimental "noise surface": an estimate of the level between stations,
 * drawn as one image over the area the stations cover.
 *
 * Each pixel is an inverse-distance average (in dB) of the stations within
 * RADIUS_M. A station's influence tapers smoothly to zero at that distance, so
 * places with no station nearby stay transparent instead of looking quiet, and
 * the surface fades out (rather than stopping at a hard edge) as stations thin.
 * Averaging in dB, not energy, keeps a loud road from flooding the quiet
 * courtyard next to it.
 *
 * The grid is laid out in Web Mercator, which is how MapLibre stretches an
 * image source between its four corners. Pixels have a fixed ground size, so
 * the surface looks the same at every zoom.
 */
window.NM = window.NM || {};

NM.surface = (function () {
  const RADIUS_M = 250; // a station informs the surface out to this distance
  const NEAR_M = 30; // closer than this, distance stops mattering (avoids 1/0)
  const CELL_M = 20; // ground size of one pixel (grows if the area is huge)
  const MAX_SIDE = 4096; // largest texture side all WebGL devices accept
  const MAX_PIXELS = 4e6;
  const EARTH_M = 40075016.686;
  const DB_MIN = 40, DB_MAX = 80;

  let lut = null; // 256 × RGB along the level scale, DB_MIN..DB_MAX
  let canvas = null;

  function buildLut() {
    const stops = NM.LEVEL_STOPS.map(([db, hex]) => [db, parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16)]);
    lut = new Uint8ClampedArray(256 * 3);
    for (let i = 0; i < 256; i++) {
      const db = DB_MIN + ((DB_MAX - DB_MIN) * i) / 255;
      let k = 0;
      while (k < stops.length - 2 && db > stops[k + 1][0]) k++;
      const [d0, ...c0] = stops[k], [d1, ...c1] = stops[k + 1];
      const t = NM.util.clamp((db - d0) / (d1 - d0), 0, 1);
      for (let c = 0; c < 3; c++) lut[i * 3 + c] = NM.util.lerp(c0[c], c1[c], t);
    }
  }

  const mercX = (lon) => (lon + 180) / 360;
  const mercY = (lat) => {
    const s = Math.sin((lat * Math.PI) / 180);
    return 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI);
  };
  const lonOf = (x) => x * 360 - 180;
  const latOf = (y) => (360 / Math.PI) * Math.atan(Math.exp((1 - 2 * y) * Math.PI)) - 90;

  /**
   * points: [{ lon, lat, db }] → { url, coordinates } for a MapLibre image
   * source, or null when there is nothing to draw.
   */
  function render(points) {
    points = points.filter((p) => Number.isFinite(p.db) && Number.isFinite(p.lon) && Number.isFinite(p.lat));
    if (!points.length) return null;
    if (!lut) buildLut();

    // Mercator units per metre, at the stations' mean latitude.
    const lat0 = points.reduce((a, p) => a + p.lat, 0) / points.length;
    const unitsPerM = 1 / (EARTH_M * Math.cos((lat0 * Math.PI) / 180));
    const r = RADIUS_M * unitsPerM;

    const pts = points.map((p) => ({ x: mercX(p.lon), y: mercY(p.lat), db: p.db }));
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const p of pts) {
      x0 = Math.min(x0, p.x); x1 = Math.max(x1, p.x);
      y0 = Math.min(y0, p.y); y1 = Math.max(y1, p.y);
    }
    x0 -= r; y0 -= r; x1 += r; y1 += r;

    let cell = CELL_M * unitsPerM;
    const fit = (c) => [Math.ceil((x1 - x0) / c), Math.ceil((y1 - y0) / c)];
    let [w, h] = fit(cell);
    const shrink = Math.max(1, w / MAX_SIDE, h / MAX_SIDE, Math.sqrt((w * h) / MAX_PIXELS));
    if (shrink > 1) { cell *= shrink; [w, h] = fit(cell); }

    // Splat each station into the pixels within its radius.
    const sumW = new Float32Array(w * h);
    const sumWdb = new Float32Array(w * h);
    const empty = new Float32Array(w * h).fill(1); // Π(1 − taper): 0 = fully covered
    const rPx = r / cell, near2 = (NEAR_M * unitsPerM / cell) ** 2;
    for (const p of pts) {
      const cx = (p.x - x0) / cell - 0.5, cy = (p.y - y0) / cell - 0.5;
      const i0 = Math.max(0, Math.floor(cx - rPx)), i1 = Math.min(w - 1, Math.ceil(cx + rPx));
      const j0 = Math.max(0, Math.floor(cy - rPx)), j1 = Math.min(h - 1, Math.ceil(cy + rPx));
      for (let j = j0; j <= j1; j++) {
        const dy = j - cy;
        for (let i = i0; i <= i1; i++) {
          const dx = i - cx;
          const d2 = dx * dx + dy * dy;
          const q = d2 / (rPx * rPx);
          if (q >= 1) continue;
          const taper = (1 - q) * (1 - q);
          const wgt = taper / (d2 + near2);
          const k = j * w + i;
          sumW[k] += wgt;
          sumWdb[k] += wgt * p.db;
          empty[k] *= 1 - taper;
        }
      }
    }

    if (!canvas) canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext('2d');
    const img = ctx.createImageData(w, h);
    const px = img.data;
    for (let k = 0; k < w * h; k++) {
      if (!sumW[k]) continue;
      const db = sumWdb[k] / sumW[k];
      const t = NM.util.clamp((db - DB_MIN) / (DB_MAX - DB_MIN), 0, 1);
      const li = Math.round(t * 255) * 3;
      const cover = 1 - empty[k];
      const o = k * 4;
      px[o] = lut[li]; px[o + 1] = lut[li + 1]; px[o + 2] = lut[li + 2];
      px[o + 3] = 255 * cover * (0.45 + 0.4 * t); // quieter → fainter, like the glow
    }
    ctx.putImageData(img, 0, 0);

    const west = lonOf(x0), east = lonOf(x0 + w * cell);
    const north = latOf(y0), south = latOf(y0 + h * cell);
    return {
      url: canvas.toDataURL('image/png'),
      coordinates: [[west, north], [east, north], [east, south], [west, south]],
    };
  }

  return { render, RADIUS_M };
})();
