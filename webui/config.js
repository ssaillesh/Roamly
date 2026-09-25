// Single source of truth for the backend API URL — set it ONCE here.
//
// Local dev → your local backend. Anything else (Vercel) → production (Render).
// After your Render service is live, replace the onrender.com URL below with
// your actual service URL (Render → your service → top of the page).
window.API_BASE =
  (location.hostname === 'localhost' || location.hostname === '127.0.0.1')
    ? 'http://127.0.0.1:8001/api/v1'
    : 'https://trekrank.onrender.com/api/v1';

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
        'max-width:calc(100% - 32px);padding:10px 16px;border-radius:999px;' +
        'background:#1c1c22;color:#f4f4f6;font:500 13px/1.3 system-ui,sans-serif;' +
        'box-shadow:0 6px 24px rgba(0,0,0,.35);pointer-events:none;transition:opacity .2s;';
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
