// Single source of truth for the backend API URL — set it ONCE here.
//
// Local dev → your local backend. Anything else (Vercel) → production (Render).
// After your Render service is live, replace the onrender.com URL below with
// your actual service URL (Render → your service → top of the page).
window.API_BASE =
  (location.hostname === 'localhost' || location.hostname === '127.0.0.1')
    ? 'http://127.0.0.1:8001/api/v1'
    : 'https://trekrank.onrender.com/api/v1';

// Where to go after signing in, from a ?redirect= value — which anyone can put in a
// link, so it's never used as is. Only a page on this site is accepted ("rova.html",
// "/admin.html", "rova.html?q=…"); another site ("//evil.com", "/\evil.com",
// "https://…") or a script URL ("javascript:", "data:", "vbscript:") gives `fallback`.
// Returns a path from the site root, e.g. "/rova.html?q=…".
window.safeRedirect = function (value, fallback) {
  fallback = fallback || '/rova.html';
  if (typeof value !== 'string' || !value || value.length > 2048) return fallback;
  // No scheme, no leading "//", no backslashes (browsers read "\" as "/") and no
  // whitespace or control characters (the URL parser drops some: "java\tscript:").
  if (/^[a-z][a-z0-9+.\-]*:/i.test(value) || value.slice(0, 2) === '//' || /[\\\s\x00-\x1f\x7f]/.test(value)) return fallback;
  try {
    const url = new URL(value, location.origin + '/');
    // Dot segments can still collapse into "//host" ("/.//evil.com"): re-check the result.
    if (url.origin !== location.origin || url.pathname.slice(0, 2) === '//') return fallback;
    return url.pathname + url.search + url.hash;
  } catch (e) {
    return fallback;
  }
};

// Wake the API as soon as any page loads. Render's free tier sleeps after
// ~15 min idle and takes ~30-60s to boot, so start that boot now — while the
// visitor is still reading — instead of on their first click.
try {
  fetch(window.API_BASE.replace(/\/api\/v1$/, '/health'), { mode: 'no-cors', cache: 'no-store' })
    .catch(() => {});
} catch (e) {}

// fetch() for API calls: aborts after a timeout (so a dead backend surfaces as
// an error instead of an endless spinner), and shows a "waking up" notice when
// a request is still pending after a few seconds. Planner calls get longer.
(function () {
  const SLOW_MS = 5000;
  let pendingSlow = 0, toast = null;

  function showToast(on) {
    if (!toast) {
      if (!on || !document.body) return;
      toast = document.createElement('div');
      toast.setAttribute('role', 'status');
      toast.textContent = 'Waking up the server… (can take up to a minute)';
      toast.style.cssText =
        'position:fixed;left:50%;bottom:20px;transform:translateX(-50%);z-index:99999;' +
        'max-width:calc(100% - 32px);padding:11px 18px;border-radius:999px;' +
        'background:#17153a;color:#fff;font:650 13.5px/1.3 "Plus Jakarta Sans",system-ui,sans-serif;' +
        'box-shadow:0 18px 40px -16px rgba(23,21,58,.55);pointer-events:none;transition:opacity .2s;';
      document.body.appendChild(toast);
    }
    toast.style.opacity = on ? '1' : '0';
  }

  window.apiFetch = function (url, opts, ms) {
    ms = ms || (/\/plan\//.test(url) ? 60000 : 20000);
    const ctrl = new AbortController();
    const kill = setTimeout(() => ctrl.abort(), ms);
    let slow = false;
    const slowTimer = setTimeout(() => { slow = true; pendingSlow++; showToast(true); }, SLOW_MS);
    return fetch(url, Object.assign({}, opts, { signal: ctrl.signal }))
      .catch(err => {
        throw err.name === 'AbortError'
          ? new Error('The server took too long to respond — try again in a minute.')
          : err;
      })
      .finally(() => {
        clearTimeout(kill); clearTimeout(slowTimer);
        if (slow && --pendingSlow === 0) showToast(false);
      });
  };
})();

// ---------------------------------------------------------------------------
// Google Analytics 4 — page views, traffic sources and the sign-up funnel
// (shown in admin.html). Paste your Measurement ID (G-XXXXXXX) below; blank
// turns analytics off. Nothing loads until the visitor accepts the cookie
// banner, and the choice is remembered. Not loaded on the admin page.
window.GA_MEASUREMENT_ID = 'G-P8639TWYYC';

// window.track('sign_up') etc. Safe to call anywhere: a no-op without consent.
window.track = function (name, params) {
  try { if (window.gtag && window.__gaOn) window.gtag('event', name, params || {}); } catch (e) {}
};

(function () {
  const ID = window.GA_MEASUREMENT_ID;
  if (!ID || /\/admin(\.html)?$/.test(location.pathname)) return;
  const KEY = 'rova_consent';
  let choice = null;
  try {
    choice = localStorage.getItem(KEY);
    // A choice made before the rename to Rova carries over, so nobody is asked twice.
    const old = localStorage.getItem('roamly_consent');
    if (old !== null){ if (choice === null){ choice = old; localStorage.setItem(KEY, old); } localStorage.removeItem('roamly_consent'); }
  } catch (e) {}

  function load() {
    window.dataLayer = window.dataLayer || [];
    window.gtag = function () { window.dataLayer.push(arguments); };
    window.gtag('js', new Date());
    window.gtag('config', ID);
    window.__gaOn = true;
    const s = document.createElement('script');
    s.async = true;
    s.src = 'https://www.googletagmanager.com/gtag/js?id=' + encodeURIComponent(ID);
    document.head.appendChild(s);
  }

  function remember(v) { try { localStorage.setItem(KEY, v); } catch (e) {} }

  function banner() {
    const el = document.createElement('div');
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-label', 'Cookie choice');
    el.style.cssText =
      'position:fixed;left:16px;right:16px;bottom:16px;z-index:99998;max-width:520px;margin:0 auto;' +
      'padding:16px 18px;border-radius:20px;background:#FBF8F3;color:#161513;' +
      'box-shadow:0 30px 64px -26px rgba(40,30,10,.38);' +
      'border:1px solid #E4DDD0;font:400 14px/1.45 "Inter",system-ui,-apple-system,sans-serif;';
    el.innerHTML =
      '<div style="margin-bottom:12px">We use Google Analytics cookies to see how people find and use Rova. ' +
      'Okay with that?</div><div style="display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap">' +
      '<button type="button" data-no style="min-height:40px;padding:0 16px;border-radius:999px;border:1px solid ' +
      '#D8D0C1;background:transparent;color:#161513;font:inherit;font-weight:500;cursor:pointer">No thanks</button>' +
      '<button type="button" data-yes style="min-height:40px;padding:0 16px;border-radius:999px;border:0;' +
      'background:#161513;color:#F4EFE6;font:inherit;font-weight:500;cursor:pointer">Accept</button></div>';
    el.querySelector('[data-yes]').onclick = () => { remember('yes'); el.remove(); load(); };
    el.querySelector('[data-no]').onclick = () => { remember('no'); el.remove(); };
    document.body.appendChild(el);
  }

  if (choice === 'yes') load();
  else if (choice !== 'no') {
    if (document.body) banner();
    else document.addEventListener('DOMContentLoaded', banner);
  }
})();
