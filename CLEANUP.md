# Codebase cleanup notes

*Audited 2026-09-28. Nothing below has been deleted yet: this is a checklist for you to work through and commit.*

Roamly started life as **TrekRank**, a travel-logging app (trips, badges, leaderboards, friends, feed, share cards). The product is now the outing planner, but most of that older code is still in the repo. This file lists what the current app doesn't use, and how safe each item is to remove.

## How "not in use" was decided

- **Import graph.** Built from everything production actually runs:
  - `app.main` (the API)
  - what `backend/start.sh` launches: the Celery worker, `scripts.seed_badges`, `scripts.reprocess_stuck`
  - Alembic
- **API calls.** Checked every call the web app makes. It uses only:
  - `/auth/register`, `/auth/login`, `/auth/forgot-password`
  - `/users/me`, `/users/me/profile`, `/users/me/profile/questions`
  - `/plan/chat`, `/plan/options`, `/plan/build`
  - `/waitlist`, `/health`
- **Deploy targets.** Frontend: Vercel (`webui/` + `vercel.json`). Backend: Render, **Docker runtime** (`backend/Dockerfile` → `start.sh`).
- **Test check.** After each tier, run `cd backend && ./run_local.sh test`. Today it gives 33 passed, 4 skipped.

---

## Tier A — safe to delete now (nothing imports or runs them)

Deleting these changes no behaviour. About 1,600 lines.

| File | Why it's unused |
|---|---|
| `backend/app/api/trips.py` | Router not mounted in `app/main.py` |
| `backend/app/api/friends.py` | Router not mounted |
| `backend/app/api/feed.py` | Router not mounted |
| `backend/app/api/leaderboards.py` | Router not mounted |
| `backend/app/api/challenges.py` | Router not mounted |
| `backend/app/api/share.py` | Router not mounted |
| `backend/app/api/dispatch.py` | Only imported by `trips.py` and `share.py` |
| `backend/app/schemas/trip.py` | Only imported by `trips.py` |
| `backend/tests/test_trips.py`, `test_feed.py`, `test_leaderboards.py`, `test_badges.py` | Each file calls `pytest.skip(...)` at the top (these are the "4 skipped") |
| `backend/scripts/seed_demo.py`, `seed_bulk.py` | Seed fake trips and leaderboards for the removed features |
| `backend/scripts/sway_hallucination_scan.py` | One-off LLM experiment. Imports `uqlm`, which isn't in `requirements.txt`. Uses an old product name ("Sway"). |
| `webui/Dockerfile`, `webui/Caddyfile` | Static hosting for Railway. The front end is on Vercel. |
| `docker-compose.yml`, `infra/` (Prometheus + Grafana) | A local "prod-like" stack with MinIO, Prometheus and Grafana. Neither Render nor Vercel uses it, and local dev uses Homebrew via `run_local.sh`. Keep it only if you want local Grafana dashboards. |
| `.DS_Store`, `backend/.DS_Store` | macOS junk that got committed. Remove them, and add `.DS_Store` to `.gitignore`. |
| `backend/media/` (local only, git-ignored) | Old share-card images on your disk. |

> If you delete `docker-compose.yml`/`infra/`, also delete the README lines that describe them.

---

## Tier B — remove together in one change (the old background worker)

About 1,265 lines, plus one dependency and several lines in `start.sh`. **Nothing in the live product sends work to the Celery worker.** The only code that enqueues tasks is `api/dispatch.py` (Tier A). The single badge call left in `users.py` runs inline, without the worker.

Yet `start.sh` still starts three things on every deploy:
- a Celery worker process
- a badge seed
- a "heal stuck trips" pass

On the 512 MB free Render instance, removing them frees memory and speeds up cold starts.

**Delete:**
- `backend/app/workers/` (the whole folder: `celery_app.py`, `trip_processor.py`, `badge_worker.py`, `share_worker.py`, `__init__.py`)
- `backend/app/services/`: `badge_evaluator.py`, `distance.py`, `friends.py`, `leaderboard.py`, `stats.py`, `share_card.py`, `storage.py`
- `backend/app/data/` (`countries.py`, used only by badges and stats)
- `backend/scripts/seed_badges.py`, `backend/scripts/reprocess_stuck.py`
- `backend/app/schemas/social.py`

**Edit at the same time:**
- **`backend/start.sh`:** remove the background block that seeds badges and heals trips, and the `celery -A app.workers worker …` line. Keep the DB-host log line and `alembic upgrade head`.
- **`backend/app/api/users.py`:**
  - Remove the travel endpoints the web app never calls: `PUT /me/featured`, `GET /search`, `GET /{username}`, `/{username}/badges`, `/{username}/stats`, `/{username}/map`.
  - In `PATCH /me`, remove the `home_changed` / `evaluate_badges_sync` branch.
  - Remove the now-unused imports (`VisitedCountry`, `VisitedCity`, `Badge`, `UserBadge`, `BadgeOut`, `detailed_stats`).
  - Keep `GET /me`, `PATCH /me` and the three `/me/profile` routes.
- **`backend/app/schemas/user.py`:** drop `UserStats`, `UserMap` and `FeaturedBadgesUpdate`, plus the stats fields on `UserProfile` if you like.
- **`backend/tests/conftest.py`:** remove the `seed()` import and the `_seed_badges` session fixture.
- **`backend/app/main.py`:** remove the `/media` static mount (share-card images only).
- **`backend/app/config.py`, `render.yaml`:** remove `storage_backend`, `local_storage_dir`, `public_base_url` and the S3/MinIO settings (`STORAGE_BACKEND`, `PUBLIC_BASE_URL` in `render.yaml`).
- **`backend/requirements.txt`:** remove `celery` and `Pillow` (share cards only). Keep `redis`: the planner still caches in Redis.
- **`backend/run_local.sh`:** remove the `worker` command.
- **`.vscode/tasks.json`:** remove the "TrekRank: Worker" task. The file still describes the old trips app.

**Check:** run the tests, then deploy and confirm the Render log shows only migrations and uvicorn, with no Celery.

---

## Tier C — the old database tables (needs a migration, don't just delete)

**Don't delete these model files on their own.**
- Migration `0001_init` builds every table from the current `app/models` with `create_all()`.
- Migration `0002_badge_emoji` reads the `badges` table without checking that it exists.
- The free Postgres is recreated every 30 days (the current one expires 2026-10-25), so migrations run on a fresh database regularly.
- If you delete the `Badge` model, the next fresh database fails at `0002`, and the API won't start.

**Models:**
- `backend/app/models/`: `trip.py`, `visited.py`, `badge.py`, `challenge.py`, `friendship.py`, `activity.py`
- The travel columns on `User`: `home_country`, `featured_badges`, `total_countries`, `total_cities`, `total_km`, `total_trips`, `current_streak`, `longest_streak`

**Safe way, following the repo's own `trip_photos` precedent in `0005`–`0007`:**
1. Add migration `0009_drop_travel_tables`. It drops each table (and the `User` columns) only if it exists, in dependency order: participants/feed/friendships/visited/user_badges before trips, badges, challenges.
2. Make `0002_badge_emoji` return early when `"badges"` isn't in `sa.inspect(bind).get_table_names()`, the same guard as `0005`. Check `0004_featured_badges` the same way.
3. Then delete the model files, and their imports in `app/models/__init__.py`.
4. Test on a throwaway empty database: `createdb roamly_fresh`, point `DATABASE_URL` at it, run `alembic upgrade head`, then run the tests.

This removes about 250 lines of models and several empty tables, and user rows get smaller. It's low value but tidy. Do it after Tier B.

---

## Tier D — your call: the paid venue APIs

About 270 lines. You set the rule that **no paid API should be in play**. These clients are wired into the planner but switched off:
- Yelp and Foursquare have keys on Render, but no card, so they're inactive.
- Google Places has no key anywhere.

Each returns nothing when inactive, so today they are dead code paths.

- `backend/app/services/yelp.py`, `foursquare.py`, `google_places.py`
- **Edit alongside:** their imports and calls in `app/services/planner.py` and `app/api/plan.py`, their settings in `app/config.py`, and the `YELP_*` / `FOURSQUARE_*` env vars on Render.
- **Benefit:** it's guaranteed that no request can ever go to a paid API, even if someone adds a card later.
- **Caution:** if you ever want Yelp photos or prices back, it's in git history.

---

## Worth fixing (not unused, but leftover history)

| What | Note |
|---|---|
| `render.yaml` | **Out of sync with the live service.** It says `runtime: python` and `trekrank-redis`; the live service is Docker-runtime with `roamly-redis`. Either update it to match, or delete it and treat the Render dashboard as the source of truth. Check first whether the service is Blueprint-managed (Render dashboard → the service → "Blueprint"). |
| `.gitignore` (root) | Only ignores `/.claude`, which is why the two `.DS_Store` files got committed. Add `.DS_Store` and `*.log`. |
| `README.md` "Tests" paragraph | Still describes "10 pytest integration tests (trips, badges, leaderboards)", which are the skipped ones. Update after Tier A/B. |
| "TrekRank" naming | Appears in the FastAPI title/description in `main.py` (it still says "Travel logging, leaderboards, badges and share cards"), the Celery app name, `.vscode/tasks.json`, and the Grafana dashboard name. Cosmetic. |

## Keep, even though they look like history

| What | Why |
|---|---|
| `backend/alembic/versions/0001`–`0008` | The live database's schema history. Never delete or renumber these. |
| `localStorage` keys `trek_token`, `trek_user`, `wander_saved` | Renaming them signs everyone out and loses their saved plans. Only change them with a migration step in the page. |
| `https://trekrank.onrender.com` | The live API URL. Render kept the subdomain after the rename. `config.js`, `keepalive.yml` and cron-job.org all use it. |
| `.github/workflows/keepalive.yml` | In use: an hourly outage alert. cron-job.org does the 5-minute keep-awake. |
| `app/metrics.py` | Used by the planner (itinerary timing metrics). |
| `backend/scripts/smoke_test.py` | Current. It tests exactly what the web app uses (register → survey → plans → reset → delete). |

## Suggested order
1. **Tier A:** delete, run the tests, commit.
2. **Tier B:** one commit. Run the tests, deploy, and check the Render log shows no Celery.
3. The `render.yaml` / `.gitignore` / README fixes.
4. **Tier C,** when you have time to test on a fresh database.
5. **Tier D,** if you want the no-paid-API rule enforced by the code itself.
