/* Stations (the boxes). Live stations are added from the lat/lon in their
 * uploads; the list below is only used by the demo feed (?demo). */
window.NM = window.NM || {};

NM.data = (function () {
  const demoStations = [
    { id: 'otaniemi-01', name: 'Otakaari', lat: 60.18676, lon: 24.82742 },
    { id: 'otaniemi-02', name: 'Alvar Aalto Park', lat: 60.18498, lon: 24.82893 },
    { id: 'otaniemi-03', name: 'Kehä I at Otaniemi', lat: 60.18257, lon: 24.81556 },
    { id: 'otaniemi-04', name: 'Otaniementie', lat: 60.1849, lon: 24.82606 },
    { id: 'otaniemi-05', name: 'Länsiväylä', lat: 60.1689, lon: 24.82904 },
    { id: 'otaniemi-06', name: 'Otaranta shore', lat: 60.184, lon: 24.8364 },
    { id: 'otaniemi-07', name: 'Teekkari Village', lat: 60.18907, lon: 24.83822 },
    { id: 'otaniemi-08', name: 'Maarintie', lat: 60.18702, lon: 24.81394 },
  ];
  const stations = NM.config.apiBase ? [] : demoStations;

  /** Add or move a live station. False if the position is missing or invalid. */
  function addStation(id, lat, lon) {
    if (!Number.isFinite(lat) || !Number.isFinite(lon) || Math.abs(lat) > 90 || Math.abs(lon) > 180) return false;
    const i = stations.findIndex((s) => s.id === id);
    if (i >= 0) Object.assign(stations[i], { lat, lon });
    else stations.push({ id, name: id, lat, lon });
    return true;
  }

  return { stations, addStation };
})();

/* Noise categories, shared by the map sheet and the meter. */
NM.category = function (db) {
  if (db < 45) return { label: 'Quiet', desc: 'Calm, like a quiet room', color: '#7457e0' };
  if (db < 56) return { label: 'Moderate', desc: 'Quiet street', color: '#b453cc' };
  if (db < 65) return { label: 'Noisy', desc: 'Steady traffic', color: '#e8526c' };
  if (db < 75) return { label: 'Loud', desc: 'Busy traffic', color: '#ff9f45' };
  return { label: 'Very loud', desc: 'Heavy traffic', color: '#ffd36a' };
};

/* Bar colour in the hourly chart (finer steps at the quiet end, as in the design). */
NM.barColor = function (db) {
  if (db < 52) return '#6f52dc';
  if (db < 60) return '#b04fc8';
  if (db < 65) return '#e5506a';
  if (db < 75) return '#ff9f45';
  return '#ffd36a';
};

/* Colour scale of the map and legend: [dB, colour] stops (the legend bar's
 * gradient in styles.css uses the same colours). */
NM.LEVEL_STOPS = [[40, '#8a5ce6'], [51.2, '#c057c4'], [60.8, '#ec5f78'], [71.2, '#ff9f45'], [80, '#ffd98a']];

/* What makes the noise: the classifier's labels (night-noise-box/python/audio.py,
 * and the model's own "noise" class) plus the demo feed's "nature". Colours are kept clear of the level scale. */
NM.SOURCES = {
  traffic: { label: 'Traffic', color: '#4cc3f0' },
  construction: { label: 'Construction', color: '#f4d35e' },
  music: { label: 'Music', color: '#f25fa8' },
  voices: { label: 'Voices', color: '#7bd88f' },
  nature: { label: 'Nature', color: '#3fae8c' },
  background: { label: 'Background', color: '#8d93a6' },
  noise: { label: 'Other noise', color: '#c9a0ff' }, // the Edge Impulse model's catch-all class
};
