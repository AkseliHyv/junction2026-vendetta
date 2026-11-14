/* Runtime configuration. Override any key by defining `window.NM_CONFIG` before
 * this script loads, or pass `?api=<base url>` in the page URL. */
window.NM = window.NM || {};

NM.config = Object.assign(
  {
    // Base URL of the readings API (see README). null → built-in demo feed.
    apiBase: null,
    // MapLibre basemap style. OpenFreeMap is free and needs no key. To use
    // MapTiler instead: 'https://api.maptiler.com/maps/dataviz-dark/style.json?key=YOUR_KEY'
    mapStyle: 'https://tiles.openfreemap.org/styles/dark',
    // Time zone used for "hour of day" (chart axis, traffic profiles, labels).
    timeZone: 'Europe/Helsinki',
    // Devices upload one analysis per window.
    windowMinutes: 15,
    // Poll this many seconds after each quarter hour, giving devices time to upload.
    pollDelaySeconds: 20,
    // A station whose newest reading is older than this is shown as offline
    // and no longer influences the map.
    staleAfterMinutes: 40,
    // Readings with less valid audio than this (out of 60 s) are ignored.
    minValidSeconds: 20,
  },
  window.NM_CONFIG || {}
);

(function () {
  const q = new URLSearchParams(location.search);
  if (q.get('api')) NM.config.apiBase = q.get('api');
  if (q.has('demo')) NM.config.apiBase = null;
})();
