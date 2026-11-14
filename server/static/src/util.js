window.NM = window.NM || {};

NM.util = {
  clamp: (v, a, b) => (v < a ? a : v > b ? b : v),
  lerp: (a, b, t) => a + (b - a) * t,
  dbToE: (db) => Math.pow(10, db / 10),
  eToDb: (e) => 10 * Math.log10(e),
  escape: (s) =>
    String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]),
};

/* Time helpers. All "hour of day" logic is in NM.config.timeZone. */
NM.time = (function () {
  const MIN = 60e3, HOUR = 3600e3;
  let fmt = null;
  let minuteShift = null;

  function formatter() {
    if (!fmt) {
      fmt = new Intl.DateTimeFormat('en-GB', {
        timeZone: NM.config.timeZone || undefined,
        hour: '2-digit',
        minute: '2-digit',
        hourCycle: 'h23',
      });
    }
    return fmt;
  }

  function hm(ms) {
    const parts = formatter().formatToParts(new Date(ms));
    const get = (t) => +parts.find((p) => p.type === t).value;
    return { h: get('hour'), m: get('minute') };
  }

  return {
    MIN,
    HOUR,
    hm,
    /** Local hour of day as a fraction, e.g. 14.5 for 14:30. */
    hourFrac(ms) {
      const t = hm(ms);
      return t.h + t.m / 60;
    },
    /** "14:45" */
    hhmm(ms) {
      const t = hm(ms);
      return String(t.h).padStart(2, '0') + ':' + String(t.m).padStart(2, '0');
    },
    /** Start of the local clock hour containing ms. */
    hourStart(ms) {
      // Local minutes − UTC minutes (mod 60): 0 for whole-hour zones, 30 for e.g. India.
      // Cached because this runs for every reading.
      if (minuteShift === null) {
        const now = Date.now();
        minuteShift = (((hm(now).m - (Math.floor(now / MIN) % 60)) % 60) + 60) % 60;
      }
      const shift = minuteShift * MIN;
      return Math.floor((ms + shift) / HOUR) * HOUR - shift;
    },
    /**
     * Start times of the 24 hourly buckets shown on the chart, indexed by clock
     * hour 0..23. Hours up to the current one are today; later hours are
     * yesterday's (i.e. the chart always covers the last 24 hours).
     */
    dayBuckets(now) {
      const cur = hm(now).h;
      const start = this.hourStart(now);
      const out = new Array(24);
      for (let h = 0; h < 24; h++) out[h] = start - ((cur - h + 24) % 24) * HOUR;
      return out;
    },
  };
})();
