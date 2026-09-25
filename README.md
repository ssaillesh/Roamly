# Roamly

An AI trip-night planner. Tell it your budget and vibe and it builds a real,
timed itinerary from live venues nearby, with a route map of the stops. The
backend service (FastAPI/Celery) is
internally named **TrekRank** — it also does travel-logging: trips, distances,
friend & global leaderboards, badges, and shareable year-in-travel cards.

```
webui (static HTML)  ──HTTP──▶  FastAPI  ──▶  PostgreSQL (PostGIS-ready)
                                        ├──▶  Redis (leaderboards, cache, rate limit)
                                        ├──▶  Celery workers (geocode, distance, badges, share cards)
                                        └──▶  Object storage (local disk in dev / S3·MinIO in prod)

Geocoding: Nominatim / OpenStreetMap (free, no API key)
```

Everything uses **free / open-source components** — no paid APIs required. Geocoding is
the free Nominatim endpoint; share-card storage defaults to the local filesystem.

---

## What's implemented

**Backend (Python 3.10+, FastAPI):** auth (email register/login + JWT refresh +
Apple sign-in stub + forgot/reset password + GDPR delete), users/profile/stats/map/search,
trips CRUD + onboarding backfill, friends (request/accept/reject/suggestions), friend &
global leaderboards (Redis sorted sets), cursor-paginated activity feed, badges (24 seeded)
+ evaluation, group challenges, Instagram-Story share-card generation, an AI planner
(chat / guided "build your own" itinerary / options endpoints backed by live venue data,
with an OpenStreetMap fallback when the keyed venue APIs come up short), and a waitlist
signup endpoint.

**3 Celery workers:** trip processor (geocode → distance → visited tables → stats →
leaderboards → feed → badge trigger), badge evaluator, share-card generator
(Pillow, 1080×1920 PNG).

**Web UI (`webui/`, static HTML/CSS/JS, no build step):**
- `index.html` — marketing landing page with an animated itinerary demo and a waitlist form.
- `auth.html` — the dedicated sign-in / create-account page: soft aurora background,
  sliding segmented toggle, live field validation, password strength meter, and a
  forgot-password flow. Any page that needs a session (the planner) routes here via
  `?redirect=`; an already-signed-in visitor is bounced straight
  through before the page even paints.
- `roamly.html` — the AI planner chat. A short conversational wizard (or free-form chat)
  builds a budget/vibe itinerary from real, open-right-now venues, with live nearby events
  woven in on request. Each itinerary card has a mini route map of its stops.
  Location sharing is a click-to-toggle pill in the header, off by default.

**Tests:** 10 pytest integration tests (trips, badges, leaderboards, feed) — 8 passing.
`test_five_countries_badge` (expects a `streak_3` badge not in the seeded catalog) and
`test_global_leaderboard` currently fail; pre-existing, unrelated to the web UI/backend
changes described above.

---

## PostGIS

Trip coordinates are stored as plain `lat`/`lng` float columns (portable across any
PostgreSQL, no PostGIS required), and the default distance calculation is a Haversine
great-circle formula in Python (`app/services/distance.py` → `distance_km`). An optional
`distance_km_postgis` helper in the same module computes the equivalent via `ST_Distance`
over `GEOGRAPHY(POINT, 4326)` for deployments that do have PostGIS enabled (the
`docker-compose.yml` Postgres image is `postgis/postgis:16-3.4`) — the two methods agree
to within ~0.5%.

---

## Run it — Option A: native (no Docker)

What this machine has: Homebrew PostgreSQL 18 + Redis (running), Python 3.10.

```bash
# prerequisites
brew services start postgresql@18
brew services start redis
createdb trekrank

cd backend
./run_local.sh setup           # venv + deps + migrate + seed badges
./run_local.sh worker          # terminal 1: Celery worker
./run_local.sh api             # terminal 2: API on http://127.0.0.1:8001
./run_local.sh smoke           # terminal 3: live end-to-end test
```

Interactive API docs: <http://127.0.0.1:8001/docs>

```bash
./run_local.sh test            # run the pytest suite
```

## Run it — Option B: full stack via Docker

Brings up Postgres+PostGIS, Redis, MinIO, Prometheus, Grafana, API, and worker:

```bash
docker compose up --build
# API on :8000, MinIO console :9001, Grafana :3000, Prometheus :9090
```

(Docker isn't installed on the current machine, so Option A is the verified path.)

---

## Web UI

Static files, no build step — served by Caddy in production (`webui/Dockerfile` +
`webui/Caddyfile`), or open directly / serve with anything static in dev.

```bash
cd webui
python3 -m http.server 5173
open http://localhost:5173
```

`webui/config.js` is the single source of truth for the API URL: it points at
`http://127.0.0.1:8001/api/v1` on `localhost`, and at the deployed backend everywhere
else — edit that file if your API lives somewhere else. `render.yaml` / `webui/Dockerfile`
describe the deployed setup (Render backend + a Railway/Caddy static site for `webui/`).

---

## Project layout

```
Sway/
├── backend/
│   ├── app/
│   │   ├── main.py              FastAPI app + middleware + static media
│   │   ├── config.py            env-driven settings
│   │   ├── database.py          SQLAlchemy engine/session
│   │   ├── models/              ORM models (users, trips, badges, …)
│   │   ├── schemas/             Pydantic request/response models
│   │   ├── api/                 routers (auth, users, trips, friends, plan, …)
│   │   ├── services/            geocoding, distance, stats, badges, leaderboard, planner, share
│   │   ├── workers/             Celery app + 3 workers
│   │   ├── middleware/          JWT auth + Redis rate limiting
│   │   └── data/countries.py    ISO country + continent reference data
│   ├── alembic/                 migrations
│   ├── scripts/                 seed_badges, smoke_test
│   ├── tests/                   pytest suite
│   └── run_local.sh             native launcher
├── webui/
│   ├── index.html            marketing landing page + waitlist
│   ├── auth.html             sign-in / create-account page
│   ├── roamly.html           AI planner chat
│   ├── config.js              API_BASE (single source of truth)
│   └── Dockerfile, Caddyfile  static-site deploy
├── docker-compose.yml            full prod-like stack (PostGIS/MinIO/Prom/Grafana)
├── render.yaml                   backend deploy config (Render)
└── infra/prometheus.yml
```

## API quick reference

`/api/v1` prefix. Highlights:

| Method | Path | Notes |
|---|---|---|
| POST | `/auth/register` `/auth/login` `/auth/refresh` | JWT |
| POST | `/auth/forgot-password` `/auth/reset-password` | password recovery |
| POST | `/trips` | returns `201` immediately; worker fills `distance_km` async |
| POST | `/trips/backfill` | bulk onboarding import |
| GET | `/users/{username}/stats` `/users/{username}/map` | detailed stats / visited-places map |
| POST | `/friends/request`, `/friends/accept/{id}` | friend graph |
| GET | `/leaderboards/friends?metric=countries&period=2026` | Redis ZSET |
| GET | `/feed` | cursor-paginated, friends only |
| GET | `/badges/me` | earned + locked |
| POST | `/share/card` | 1080×1920 PNG |
| POST | `/plan/chat` `/plan/options` `/plan/build` | AI itinerary planner (chat / picker) |
| POST | `/waitlist` | early-access signup |

Full interactive docs at `/docs`.
