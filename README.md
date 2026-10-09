# Roamly

An AI outing planner. Tell it what you're after — or tap the controls — and it builds a
real, timed plan from live venues and events nearby, with a route map of the stops. See
[PRODUCT.md](PRODUCT.md) for how it works and how people use it.

```
webui (static HTML, Vercel)  ──HTTP──▶  FastAPI (Render)  ──▶  PostgreSQL
                                                 ├──▶  Redis (planner cache, rate limit)
                                                 └──▶  free venue/event sources (OpenStreetMap,
                                                        Ticketmaster free tier, City of Toronto feed)
```

Everything uses **free / open-source components** — no paid APIs. (The backend was once a
travel-logging app called **TrekRank**; that code and its tables were removed — migration
`0009` drops the tables. Some names, like the `trekrank.onrender.com` URL, remain.)

---

## What's implemented

**Backend (Python 3.10+, FastAPI):** auth (email register/login, JWT refresh,
forgot/reset/change password, account deletion), the current user (`/users/me`), an AI
planner (chat / "pick my spots" builder / options, backed by live venue data with an
OpenStreetMap fallback, a 25-second time budget and live events), a taste profile
(14-question optional survey whose answers become planner defaults — crew, budget, pace,
crowds, energy, food rules, loves & hard nopes, no-alcohol), and a waitlist signup
endpoint, plus an admin dashboard API (`/admin/*`, see below). No background worker:
everything runs in the API process, with Redis for caching and rate limiting.

**Web UI (`webui/`, static HTML/CSS/JS, no build step):**
- `index.html` — marketing landing page with an animated itinerary demo and a waitlist form.
- `auth.html` — the dedicated sign-in / create-account page: soft aurora background,
  sliding segmented toggle, live field validation, password strength meter, and a
  forgot-password flow. Any page that needs a session (the planner) routes here via
  `?redirect=`; new accounts continue to the taste survey; an already-signed-in visitor is bounced straight
  through before the page even paints.
- `profile.html` — **My taste**: a summary of the taste profile (tap any row to change just that
  answer) plus the optional 14-question survey, autosaved per answer, ending in a profile result
  ("🌙 Cozy Foodie Explorer").
- `roamly.html` — the AI planner chat, built as *chat + quick controls*. A **Planning around**
  bar (📍 where · 📏 radius · 💸 budget · 🕖 when · 👥 who · ✨ mood · 🚶 transport) shows what
  Roamly is working with; tapping it opens one settings sheet, and typed requests update it
  (changed values flash). The home screen offers ✨ Plan my night · 🔴 What's on now · 🎲 Surprise
  me; tapped settings skip the LLM entirely. Live location is opt-in. Plans come from real nearby
  venues with a mini route map; "what's on" lists live events (Ticketmaster + the City of
  Toronto festivals feed) with on-now status, free/price badges and *plan around it*.
  Views: Home · Chat · What's on · Saved (bottom nav on phones, sidebar on tablet/desktop; on
  desktop the settings are an always-open side panel). Typed messages send the bar's settings as
  `defaults`, so what you type wins, the bar fills the gaps, and "Roamly heard" shows what changed.
- `admin.html` — the **admin dashboard** (admins only): users & retention, sign-up funnel,
  traffic sources, where users are, planner quality, taste insights, server health, outside-API
  usage, user management, waitlist invites, admin activity and CSV exports.
- `roamly.css` — the shared design system (tokens, buttons, chips, cards, sheet, nav) used by the
  planner, My taste and sign-in pages.

**Tests:** `./run_local.sh test` — pytest against the local Postgres + Redis (auth security,
taste profile, planner time budget, radius, live events, session defaults, admin dashboard).
`./run_local.sh smoke` runs a live end-to-end check against a running API.

---

## Run it — Option A: native (no Docker)

What this machine has: Homebrew PostgreSQL 18 + Redis (running), Python 3.10.

```bash
# prerequisites
brew services start postgresql@18
brew services start redis
createdb trekrank

cd backend
./run_local.sh setup           # venv + deps + migrate
./run_local.sh api             # API on http://127.0.0.1:8001
./run_local.sh smoke           # (another terminal) live end-to-end test
```

Interactive API docs: <http://127.0.0.1:8001/docs>

```bash
./run_local.sh test            # run the pytest suite
```

`docker-compose.yml` and `infra/` (Prometheus/Grafana/MinIO) are a legacy local stack that
is no longer maintained — see [CLEANUP.md](CLEANUP.md).

---

## Web UI

Static files, no build step — deployed on Vercel (`webui/vercel.json`); serve with
anything static in dev.

```bash
cd webui
python3 -m http.server 5173
open http://localhost:5173
```

`webui/config.js` is the single source of truth for the API URL: it points at
`http://127.0.0.1:8001/api/v1` on `localhost`, and at the deployed backend everywhere
else — edit that file if your API lives somewhere else. The backend deploys to Render
(Docker runtime, `backend/Dockerfile` → `backend/start.sh`: migrations, then uvicorn).

---

## Admin dashboard

`webui/admin.html`, backed by `/api/v1/admin/*`. To become an admin, sign up in the app,
then run from `backend/` (locally, or in Render's shell for production):

```bash
.venv/bin/python -m scripts.make_admin you@example.com          # --remove to take it away
```

There's no API to make someone an admin. The "Admin dashboard" link then shows up in the
planner's account menu.

**What it records** (since migration `0010`; nothing before that can be filled in):
- **Active days:** one row per user per day (`user_activity_days`), for DAU/WAU/MAU and retention.
- **Planner requests:** every request with what was asked and the answer (`plan_logs`), kept 90 days.
  Older days are rolled up into `daily_rollups` first.
- **Server stats:** per-route request counts, errors and latency, saved once a minute
  (`health_snapshots`, 90 days). Unlike Prometheus' in-memory counters, these survive Render
  sleeping the server.
- **Outside APIs:** daily calls per provider (`api_usage`), checked against free-tier limits
  in `services/usage.py`.
- **Admin actions:** every action taken in the dashboard (`admin_audit_log`).

Test accounts (`@example.com` and similar, `smoke_*`) are flagged `is_test` and left out of
the stats unless the "Test accounts" toggle is on.

**Optional settings:**
- **Google Analytics 4** (visitors, traffic sources, website funnel steps):
  - Set `GA_MEASUREMENT_ID` in `webui/config.js`. Visitors get a cookie banner, and nothing loads until they accept.
  - On the API, set `GA4_PROPERTY_ID` and `GA4_CREDENTIALS_JSON`: a service-account key with Viewer access on the property.
- **Waitlist invites and admin password resets** need `SENDGRID_API_KEY` and `EMAIL_FROM`.
- **`METRICS_TOKEN`** lets a Prometheus scraper read `/metrics`. Otherwise `/metrics` is admin-only, and so is `/plan/debug`.

**Paid products are off:** Foursquare, Yelp and Google Places are switched off in code (`DISABLED` in `backend/app/services/usage.py`). They're never called, even with a key set.

**Free-tier blocker:** while `FREE_TIER_ONLY` is on (the default), the API never calls a metered free service (Groq, SendGrid, Ticketmaster, Open-Meteo) past a cap just under its free allowance. Caps and notes are in `FREE_TIER` in the same file; usage is shown under **Outside APIs** in the dashboard.

---

## Project layout

```
Sway/
├── backend/
│   ├── app/
│   │   ├── main.py              FastAPI app + middleware
│   │   ├── config.py            env-driven settings
│   │   ├── database.py          SQLAlchemy engine/session
│   │   ├── models/              ORM models (users, taste profiles, waitlist, analytics)
│   │   ├── schemas/             Pydantic request/response models
│   │   ├── api/                 routers (auth, users, plan, waitlist, admin)
│   │   ├── services/            planner, venue/event sources, geocoding, weather, LLM, taste profile
│   │   └── middleware/          JWT auth, Redis rate limiting, request stats
│   ├── alembic/                 migrations
│   ├── scripts/                 smoke_test, make_admin
│   ├── tests/                   pytest suite
│   └── run_local.sh             native launcher
├── webui/
│   ├── index.html            marketing landing page + waitlist
│   ├── auth.html             sign-in / create-account page
│   ├── roamly.html           AI planner (home · chat · what's on · saved)
│   ├── profile.html          My taste + taste survey
│   ├── admin.html            admin dashboard
│   ├── roamly.css            shared design system
│   ├── config.js              API_BASE (single source of truth)
│   └── vercel.json            static-site deploy (Vercel)
├── docker-compose.yml            legacy local stack (see CLEANUP.md)
├── render.yaml                   backend deploy config (Render)
└── infra/prometheus.yml
```

## API quick reference

`/api/v1` prefix. Highlights:

| Method | Path | Notes |
|---|---|---|
| POST | `/auth/register` `/auth/login` `/auth/refresh` | JWT |
| POST | `/auth/forgot-password` `/auth/reset-password` | password recovery |
| GET/PATCH | `/users/me` | the signed-in account |
| POST | `/plan/chat` `/plan/options` `/plan/build` | AI itinerary planner (chat / picker); optional structured `prefs` |
| GET/PUT | `/users/me/profile` · GET `/users/me/profile/questions` | taste survey (partial saves merge) |
| POST | `/waitlist` | early-access signup |
| GET | `/admin/overview` `/admin/planner` `/admin/health` … | admin dashboard (admins only) |

Full interactive docs at `/docs`.
