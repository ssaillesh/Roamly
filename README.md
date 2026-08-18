# Sway

A chat-driven itinerary planner. Tell it your budget and the vibe, and it builds
a real, timed plan from venues that actually exist and are open — sequenced so
the stops are close together, fit the budget, and include a hidden gem.

```
webui/roamly.html  ──▶  FastAPI  ──▶  planner  ──▶  Yelp / Foursquare / Google Places
  (chat UI)             /plan/*                     OpenStreetMap / Ticketmaster
                           │                        Nominatim (geocode) · Open-Meteo
                           │
                        Postgres (accounts)  ·  Redis (venue + geocode cache)
```

## What it does

**`POST /plan/chat`** — the concierge. Reads the whole conversation, extracts
budget / vibe / party size / transport / dietary / interests / avoid / named
venues, asks one focused follow-up when something essential is missing, then
returns a full itinerary. With `LLM_API_KEY` set it uses an LLM for extraction
and narration; without one it falls back to a keyword heuristic and still works.

**`POST /plan/options`** — build-your-own. Ranked, tagged candidate venues per
category (🔥 trending / ⭐ top-rated / 💎 hidden gem / 😴 quiet) plus live events.

**`POST /plan/build`** — assembles, sequences and narrates an itinerary from the
venues the user picked.

**`GET /plan/debug`** — which venue sources and the LLM are configured. Never
returns keys.

All three planning routes require a signed-in account (`auth.html`).

## Venue sourcing

Candidates are gathered from every configured source, deduped by name, then
scored on rating, closeness to the anchor, price fit, a hidden-gem bonus and a
"fun factor" derived from category. Transport mode sets the search radius
(walk 1.6km / transit 7km / car 9km) and the distance penalty.

Every source is optional. With **no keys at all** the planner still runs on
OpenStreetMap (Overpass) + Nominatim, both free and keyless.

| Source | Key | Gives |
|---|---|---|
| OpenStreetMap Overpass | — | venues, always on |
| Nominatim | — | geocoding, place verification |
| Open-Meteo | — | weather line on the plan |
| Yelp Fusion | `YELP_API_KEY` | ratings, price, photos |
| Foursquare | `FOURSQUARE_API_KEY` | popularity / foot-traffic signal |
| Google Places | `GOOGLE_PLACES_API_KEY` | ratings, price, photos |
| Ticketmaster | `TICKETMASTER_API_KEY` | live events near the plan |
| Any OpenAI-compatible LLM | `LLM_API_KEY` | extraction + narration |

Responses are cached in Redis (venues 7 days, geocodes 30 days), which keeps the
free tiers well inside their quotas.

## Run it locally

Needs Postgres and Redis running, plus Python 3.10+.

```bash
brew services start postgresql@18
brew services start redis
createdb sway

cd backend
./run_local.sh setup     # venv, deps, migrate
./run_local.sh api       # API on :8001

cd ../webui              # any static server
python3 -m http.server 8080
```

Then open <http://127.0.0.1:8080/>. That's the landing page; "Plan tonight"
takes you through sign-up into the planner, which is members-only. If you're
already signed in, the landing page's buttons jump straight to the planner.

Configure keys in `backend/.env` (copy `.env.example`). None are required.

## Layout

```
backend/app/
  api/plan.py         the three planning routes + preference extraction
  api/auth.py         register / login / refresh / reset
  services/planner.py candidate gathering, scoring, sequencing, narration
  services/           llm, yelp, foursquare, google_places, hotspots (OSM),
                      events, weather, geocoding, experience, security, email
  models/user.py      the only table
webui/
  index.html          landing page — what it does, why, and the CTA into sign-up
  auth.html           sign in / register
  roamly.html         the chat planner (self-contained: HTML + CSS + JS)
  config.js           API base URL — the one place to set it
```

Each page is a single self-contained file — no build step, no framework, no
external requests beyond the API. `index.html → auth.html → roamly.html` is the
whole flow.

## Known issues

Carried over from the previous version of this app and **not yet fixed**:

- `POST /auth/apple` does not verify the Apple identity token. It will issue a
  valid session for any account given only its email address. Disable the route
  or implement JWKS verification before exposing this publicly.
- With `SENDGRID_API_KEY` unset, `POST /auth/forgot-password` returns the reset
  token in the response body, which is a full account-takeover path. Configure
  email, or delete that fallback branch.
- `JWT_SECRET` silently defaults to a public value. `render.yaml` generates a
  real one; nothing else does.
- There are no tests. The previous suite covered only removed features.
