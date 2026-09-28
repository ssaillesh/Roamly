# Codebase cleanup notes

Roamly started life as **TrekRank**, a travel-logging app (trips, badges, leaderboards, friends, feed, share cards). This file tracks removing that history, so it doesn't affect the outing planner.

**How "not in use" was decided:**
- **Import graph.** Built from what production runs: the API, `start.sh` and Alembic.
- **API calls.** Checked the endpoints the web app calls:
  - `/auth/register`, `/auth/login`, `/auth/forgot-password`
  - `/users/me` and `/users/me/profile*`
  - `/plan/chat`, `/plan/options`, `/plan/build`
  - `/waitlist`, `/health`
- **Deploy targets.** Frontend: Vercel. Backend: Render, Docker runtime (`backend/Dockerfile` → `start.sh`).

---

## ✅ Done — database migration (2026-09-29, not yet committed)

**New migration `backend/alembic/versions/0009_drop_travel_tables.py`:**
- **Drops 9 tables:** `activity_feed`, `user_badges`, `challenge_participants`, `friendships`, `visited_cities`, `visited_countries`, `challenges`, `badges`, `trips`. It drops children before parents, following the real foreign keys.
- **Drops 9 `users` columns:** `apple_id` (Apple sign-in was removed earlier), `home_country`, `featured_badges`, `total_countries`, `total_cities`, `total_km`, `total_trips`, `current_streak`, `longest_streak`.
- **Idempotent:** it only drops what exists.
- **One-way:** downgrade refuses, because the rows can't be rebuilt from code. Restore a backup instead.

**Migration `0002_badge_emoji`** now skips when `badges` doesn't exist. This follows the same guard `0005` uses for `trip_photos`. Without it, a fresh database would crash at `0002`.

**Deleted (the badge feature):**
- `app/models/badge.py`
- `app/services/badge_evaluator.py`
- `app/workers/badge_worker.py`
- `scripts/seed_badges.py`
- `tests/test_badges.py`

**Live code no longer references anything old:**

| File | Change |
|---|---|
| `app/models/__init__.py` | Registers only `User`, `TasteProfile`, `WaitlistSignup` |
| `app/models/user.py` | The 9 dropped columns removed. **Must deploy with the migration**, which `start.sh` guarantees because it migrates before starting the API. |
| `app/api/users.py` | Keeps `GET/PATCH /me` and the three `/me/profile` routes. Removed: featured badges, user search, public profiles, badges, stats, map. |
| `app/schemas/user.py` | Just the account fields |
| `app/main.py` | `/media` mount removed; title and description now say Roamly |
| `app/config.py` | Share-card storage / S3 / MinIO settings removed. Old env vars are ignored (`extra="ignore"`). |
| `start.sh` | Migrations, then uvicorn. **No Celery worker, badge seeding or trip healing**, so less memory on the 512 MB instance. |
| `requirements.txt` | `celery` and `Pillow` removed (`redis` stays for the planner cache) |
| `tests/conftest.py` | Badge seeding and the trip-processor/leaderboard fixtures removed |
| `run_local.sh`, `.vscode/tasks.json`, `render.yaml`, `README.md`, `PRODUCT.md` | Worker and badge references removed |

**Verified:**
- **Existing database:** the local DB went `0008` → `0009`. All 474 users, 67 taste profiles and 2 waitlist signups kept. A backup was taken first: a `pg_dump` in the session scratchpad.
- **Fresh empty database:** the full chain `0001` → `0009` runs cleanly and leaves only `users`, `user_profiles`, `waitlist_signups` (plus PostGIS's `spatial_ref_sys`). Re-running is a no-op.
- **Tests:** `pytest` gives 33 passed, 3 skipped.
- **Smoke test:** `scripts/smoke_test.py` passes: register → survey → plans → reset → delete.
- **Browser:** sign-up, survey, My taste and planner flows pass, with no JS errors.

### ⚠️ What happens when you push
Render runs `alembic upgrade head` on start, so **`0009` drops those tables on the production database on the first deploy.** Everything the app uses is kept (accounts, taste profiles, waitlist). I haven't looked inside the production database. Since those routes have been switched off, expect little more than the 24 seeded badges there.

If you want a copy first: Render dashboard → Roamly-db → **Backups**, or run `pg_dump "<external database URL>" -Fc -f roamly-before-0009.dump`.

---

## ⏳ Left for you — delete the leftover old files

These are now **orphaned**. Nothing imports them, and several import modules that no longer exist, so they couldn't run anyway. (The automated delete was blocked, so this step is yours.) From the repo root:

```bash
git rm -r backend/app/workers backend/app/data \
  backend/app/api/{trips,friends,feed,leaderboards,challenges,share,dispatch}.py \
  backend/app/schemas/{trip,social}.py \
  backend/app/services/{distance,friends,leaderboard,stats,share_card,storage}.py \
  backend/app/models/{trip,visited,challenge,friendship,activity}.py \
  backend/scripts/{reprocess_stuck,seed_demo,seed_bulk}.py \
  backend/tests/test_{trips,feed,leaderboards}.py
git add -A backend render.yaml README.md PRODUCT.md CLEANUP.md .vscode
cd backend && ./run_local.sh test      # expect: 33 passed
```

`git rm` stages the deletions for your commit. The files stay in git history if you ever need them.

---

## Still open (not required by the migration)

**Safe to delete:** nothing runs them.
- `docker-compose.yml` and `infra/` (Prometheus/Grafana/MinIO). A legacy local stack. Its `worker` service can no longer start (Celery is gone).
- `webui/Dockerfile`, `webui/Caddyfile`. Railway static hosting; the front end is on Vercel.
- `backend/scripts/sway_hallucination_scan.py`. A one-off LLM experiment. It needs `uqlm`, which isn't in `requirements.txt`.
- `.DS_Store`, `backend/.DS_Store`. macOS junk that got committed. Also add `.DS_Store` to the root `.gitignore`, which today only ignores `/.claude`.
- `backend/media/` (local only, git-ignored). Old share-card images.
- `app/metrics.py`: its PostGIS query-timing metric was only used by the old `distance.py`. The itinerary metric is still used, so keep the file.

**Your call: the paid venue APIs** (Yelp, Foursquare, Google Places). Wired into the planner but inactive (no card / no key). Removing them means:
- deleting `backend/app/services/{yelp,foursquare,google_places}.py`
- editing their imports and calls in `app/services/planner.py` and `app/api/plan.py`
- removing their settings in `app/config.py` and the `YELP_*` / `FOURSQUARE_*` env vars on Render

**Worth fixing:**
- **`render.yaml`** is out of sync with the live service. It says `runtime: python` and `trekrank-redis`; the live service is Docker-runtime with `roamly-redis`. Update it or delete it (check the service isn't Blueprint-managed first).
- **Naming:** "TrekRank" still appears in cosmetic places (`/health` says `"app":"TrekRank"` via `settings.app_name`, `.vscode/tasks.json`, the Grafana dashboard).

## Keep, even though they look like history

| What | Why |
|---|---|
| `backend/alembic/versions/0001`–`0009` | The live database's schema history. Never delete or renumber these. |
| `localStorage` keys `trek_token`, `trek_user`, `wander_saved` | Renaming them signs everyone out and loses their saved plans. |
| `https://trekrank.onrender.com` | The live API URL. `config.js`, `keepalive.yml` and cron-job.org use it. |
| `.github/workflows/keepalive.yml` | In use: an hourly outage alert. |
| `backend/scripts/smoke_test.py` | Current: tests exactly what the web app uses. |
