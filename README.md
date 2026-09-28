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
endpoint. No background worker: everything runs in the API process, with Redis for
caching and rate limiting.

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
- `roamly.css` — the shared design system (tokens, buttons, chips, cards, sheet, nav) used by the
  planner, My taste and sign-in pages.

**Tests:** `./run_local.sh test` — pytest against the local Postgres + Redis (auth security,
taste profile, planner time budget, radius, live events, session defaults).
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

## Project layout

```
Sway/
├── backend/
│   ├── app/
│   │   ├── main.py              FastAPI app + middleware
│   │   ├── config.py            env-driven settings
│   │   ├── database.py          SQLAlchemy engine/session
│   │   ├── models/              ORM models (users, taste profiles, waitlist)
│   │   ├── schemas/             Pydantic request/response models
│   │   ├── api/                 routers (auth, users, plan, waitlist)
│   │   ├── services/            planner, venue/event sources, geocoding, weather, LLM, taste profile
│   │   └── middleware/          JWT auth + Redis rate limiting
│   ├── alembic/                 migrations
│   ├── scripts/                 smoke_test
│   ├── tests/                   pytest suite
│   └── run_local.sh             native launcher
├── webui/
│   ├── index.html            marketing landing page + waitlist
│   ├── auth.html             sign-in / create-account page
│   ├── roamly.html           AI planner (home · chat · what's on · saved)
│   ├── profile.html          My taste + taste survey
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

Full interactive docs at `/docs`.
