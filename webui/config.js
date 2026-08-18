// Single source of truth for the backend API URL — set it ONCE here.
//
// Local dev → your local backend. Anything else (Vercel) → production (Render).
// After your Render service is live, replace the onrender.com URL below with
// your actual service URL (Render → your service → top of the page).
window.API_BASE =
  (location.hostname === 'localhost' || location.hostname === '127.0.0.1')
    ? 'http://127.0.0.1:8001/api/v1'
    : 'https://sway-api.onrender.com/api/v1';
