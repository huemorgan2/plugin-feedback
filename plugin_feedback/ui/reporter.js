/* plugin-feedback error reporter (plan 007) — injected into every proxied
 * page by the luna-service proxy. Captures browser-side failures (JS errors,
 * unhandled rejections, resource-load failures, failed/slow fetches) and
 * posts batches to the plugin's /errors route with the Shell's bearer token.
 * Entirely best-effort: it must never throw, loop, or slow the page.
 */
(function () {
  'use strict';
  if (window.__lunaErrorReporter) return;
  window.__lunaErrorReporter = true;

  // /a/<slug>/... when proxied through luna-service; '' on direct access.
  var m = /^\/a\/[^/]+/.exec(window.location.pathname);
  var BASE = m ? m[0] : '';
  var ENDPOINT = BASE + '/api/p/plugin-feedback/errors';

  var MAX_BATCH = 20;
  var FLUSH_MS = 10000;
  var DEDUPE_MS = 30000;
  var MAX_PER_MINUTE = 60;
  var SLOW_MS = 15000;

  var queue = [];
  var crumbs = [];
  var seen = {}; // dedupe key -> last sent ts
  var minuteStart = 0;
  var minuteCount = 0;
  var dead = false; // set on 401/403 — stop trying for this page

  // -- scrubbing ---------------------------------------------------------
  var TOKEN_RES = [
    /\blsv1-[A-Za-z0-9_\-]{8,}/g,
    /\bsk-[A-Za-z0-9_\-]{16,}/g,
    /\bghp_[A-Za-z0-9]{20,}/g,
    /\bxox[a-z]-[A-Za-z0-9\-]{10,}/g,
    /\bwhsec_[A-Za-z0-9]{16,}/g,
    /\bAKIA[0-9A-Z]{16}\b/g,
    /\beyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}/g
  ];
  function scrub(text) {
    if (typeof text !== 'string') return text;
    for (var i = 0; i < TOKEN_RES.length; i++) {
      text = text.replace(TOKEN_RES[i], '[redacted]');
    }
    return text.replace(
      /([A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD)[A-Z0-9_]*\s*[=:]\s*)(\S+)/g,
      '$1[redacted]'
    );
  }
  function cleanUrl(url) {
    if (typeof url !== 'string') return url;
    return scrub(url.split('#')[0].split('?')[0]);
  }

  // -- breadcrumbs -------------------------------------------------------
  function crumb(type, detail) {
    try {
      crumbs.push({ t: type, d: String(detail).slice(0, 120), at: new Date().toISOString() });
      if (crumbs.length > 20) crumbs.shift();
    } catch (e) { /* never throw */ }
  }
  document.addEventListener('click', function (e) {
    var el = e.target && e.target.closest ? (e.target.closest('a,button') || e.target) : e.target;
    if (!el || !el.tagName) return;
    var label = el.tagName.toLowerCase();
    if (el.id) label += '#' + el.id;
    var text = (el.textContent || '').trim().slice(0, 40);
    crumb('click', label + (text ? ' "' + text + '"' : ''));
  }, true);
  window.addEventListener('popstate', function () { crumb('nav', location.pathname); });
  ['pushState', 'replaceState'].forEach(function (fn) {
    var orig = history[fn];
    if (!orig) return;
    history[fn] = function () {
      try { crumb('nav', arguments[2] || location.pathname); } catch (e) {}
      return orig.apply(this, arguments);
    };
  });

  // -- queue + transport -------------------------------------------------
  function allowed(kind, message) {
    var now = Date.now();
    if (now - minuteStart > 60000) { minuteStart = now; minuteCount = 0; }
    if (++minuteCount > MAX_PER_MINUTE) return false;
    var key = kind + '|' + String(message).slice(0, 200);
    if (seen[key] && now - seen[key] < DEDUPE_MS) return false;
    seen[key] = now;
    return true;
  }

  function report(kind, severity, message, context) {
    try {
      if (dead || !allowed(kind, message)) return;
      var ctx = context || {};
      ctx.url = cleanUrl(location.pathname);
      ctx.user_agent = navigator.userAgent;
      ctx.breadcrumbs = crumbs.slice();
      queue.push({
        source: 'ui',
        kind: kind,
        severity: severity,
        message: scrub(String(message)).slice(0, 500),
        occurred_at: new Date().toISOString(),
        context: ctx
      });
      if (queue.length >= MAX_BATCH) flush();
    } catch (e) { /* never throw */ }
  }

  function flush() {
    if (dead || !queue.length) return;
    var events = queue.splice(0, MAX_BATCH);
    var token = null;
    try { token = window.localStorage.getItem('luna.token'); } catch (e) {}
    if (!token) return; // unauthenticated page (login screen) — drop quietly
    try {
      var headers = { 'content-type': 'application/json', 'authorization': 'Bearer ' + token };
      window.__lunaReporterFetch(ENDPOINT, {
        method: 'POST',
        headers: headers,
        body: JSON.stringify({ events: events }),
        keepalive: true
      }).then(function (resp) {
        if (resp && (resp.status === 401 || resp.status === 403)) dead = true;
      }).catch(function () { /* drop — telemetry is best-effort */ });
    } catch (e) { /* never throw */ }
  }
  setInterval(flush, FLUSH_MS);
  window.addEventListener('pagehide', flush);

  // -- feed 1: JS errors + resource-load failures ------------------------
  window.addEventListener('error', function (e) {
    try {
      if (e.target && e.target !== window && (e.target.src || e.target.href)) {
        var target = cleanUrl(String(e.target.src || e.target.href));
        report('resource_error', 'warning',
          'failed to load ' + (e.target.tagName || '?').toLowerCase() + ': ' + target,
          { target: target });
        return;
      }
      var ctx = { line: e.lineno, col: e.colno, file: cleanUrl(e.filename || '') };
      if (e.error && e.error.stack) ctx.stack = scrub(String(e.error.stack)).slice(0, 16000);
      report('js_error', 'error', e.message || 'unknown script error', ctx);
    } catch (err) { /* never throw */ }
  }, true);

  // -- feed 2: unhandled promise rejections ------------------------------
  window.addEventListener('unhandledrejection', function (e) {
    try {
      var reason = e.reason;
      var message = reason && reason.message ? reason.message : String(reason);
      var ctx = {};
      if (reason && reason.stack) ctx.stack = scrub(String(reason.stack)).slice(0, 16000);
      report('unhandled_rejection', 'error', message, ctx);
    } catch (err) { /* never throw */ }
  });

  // -- feed 3: fetch failures / 5xx / slow requests ----------------------
  // Keep an unwrapped reference for our own transport so a failing sink
  // can never observe itself.
  window.__lunaReporterFetch = window.fetch.bind(window);
  var origFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    var url = '';
    try { url = typeof input === 'string' ? input : (input && input.url) || ''; } catch (e) {}
    if (url.indexOf('/api/p/plugin-feedback/errors') !== -1) {
      return origFetch(input, init);
    }
    var method = (init && init.method) || (input && input.method) || 'GET';
    var started = Date.now();
    crumb('fetch', method + ' ' + cleanUrl(url));
    return origFetch(input, init).then(function (resp) {
      var ms = Date.now() - started;
      try {
        if (resp.status >= 500) {
          report('http_5xx', 'error', method + ' ' + cleanUrl(url) + ' -> ' + resp.status,
            { target: cleanUrl(url), method: method, status: resp.status, latency_ms: ms });
        } else if (ms > SLOW_MS) {
          report('timeout', 'warning', method + ' ' + cleanUrl(url) + ' took ' + ms + 'ms',
            { target: cleanUrl(url), method: method, status: resp.status, latency_ms: ms });
        }
      } catch (e) { /* never throw */ }
      return resp;
    }, function (err) {
      try {
        report('fetch_error', 'error',
          method + ' ' + cleanUrl(url) + ' failed: ' + (err && err.message ? err.message : err),
          { target: cleanUrl(url), method: method, latency_ms: Date.now() - started });
      } catch (e) { /* never throw */ }
      throw err;
    });
  };
})();
