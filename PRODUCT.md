# Roamly: product overview

**Roamly is an AI outing planner.** Tell it what you're in the mood for, and it builds a real, timed plan from places near you. Or ask what's happening right now, and it lists live events around you. It's for date nights, days out with friends, solo adventures, family outings, and "I'm bored, what should I do?"

> **How to read this document.** It describes the product as it's meant to be, not only what exists today. Every feature is marked:
> - ✅ **Built:** works today (locally, and live once pushed)
> - 🟡 **Partly built:** works, with gaps noted
> - 🔜 **Planned:** agreed direction, not built yet
>
> For setup, running and the API, see [README.md](README.md).

---

## 1. The idea in one line

> **Chat when you want to talk. Tap when you want to control.**

Planning apps usually fall into one of two traps: a long form of questions, or a chatbot that makes you explain everything in words. Roamly does both jobs:
- **You can just talk:** "cheap sushi date in Kensington", or "what's on tonight?"
- **You can see and adjust what it's working with.** A slim **Planning around** bar shows the settings it's using (where, how far, budget, when, who, mood, getting there), and one tap changes any of them.

The bar is a live picture of what Roamly has understood. When you type something, the bar updates, and the values that changed flash briefly. So you can check that it understood you correctly, at a glance.

---

## 2. Two kinds of knowledge: *who you are* vs *what you want now*

| | **Your taste** (who you are) | **This session** (what you want now) |
|---|---|---|
| Examples | Usually go out with your partner, love Japanese food, no alcohol, prefer quiet places | Tonight, $80, within 4 km, with 3 friends |
| Where it lives | 🧠 **My taste**, a saved survey on your account | The **Planning around** bar |
| How often it changes | Rarely | Every time |
| How you change it | Retake or edit the survey | Tap a chip, open the settings, or just type |

**Rules for combining them:**
1. **What you say now beats your profile.** "With 4 friends" overrides "usually with partner".
2. **A hard "never" beats a love.** If something is both, it's treated as a "never".
3. **Something you explicitly ask for beats a profile "never".** If your profile says no nightlife but you type "dance clubs tonight", you get dance clubs.
4. **Your profile only fills gaps.** You never have to repeat your usual preferences.
5. ✅ **Start over** in the menu resets the session to your profile defaults, so yesterday's "cheap date" doesn't carry into today's "birthday dinner".

---

## 3. Who it's for, and what they come to do

| Situation | What the user does | Status |
|---|---|---|
| "Plan my date night" | Taps **✨ Plan my night**, or types it | ✅ |
| "Day out with friends, $60 each" | Types it, or sets 👥 Friends · 4 and 💸 $240 in the bar | ✅ |
| "What's happening around me right now?" | Taps **🔴 What's on now**, or asks | ✅ Ticketmaster ([§8](#8-whats-on-now-live-events)) |
| "Anything free on this weekend?" (festivals, TIFF street events) | Asks "any festivals this weekend?" | 🟡 The source exists but the city's feed is down ([§8](#8-whats-on-now-live-events)) |
| "I don't care, surprise me" | Taps **🎲 Surprise me** | ✅ |
| "I want to pick the places myself" | Taps **🧩 Pick the spots myself** | ✅ |
| "Just find me somewhere to eat now" | 🍽️ **Eat now** mode: three quick picks, not a full plan | 🔜 |
| "I'm bored" | 🥱 **I'm bored** mode: three instant ideas from the time, weather and events | 🔜 |
| "Plan a weekend trip" | Types "2-day trip…" (multi-day plans exist); a ✈️ **Trip** mode is coming | 🟡 |

---

## 4. The user journey

### 4.1 First visit
1. **Landing page** (`index.html`) ✅: what Roamly does, a demo, and an early-access waitlist.
2. **Sign up / sign in** (`auth.html`) ✅: email and password. Anything that needs an account sends you here first and brings you back afterwards.
3. **Taste survey** (`profile.html`) ✅, **optional**:
   - New accounts are offered it; **Skip for now** is always there.
   - Answers save as you go, so you can leave and pick up where you left off.
   - It ends with a fun result, e.g. "🌙 Cozy Foodie Explorer".
   - Afterwards **My taste** shows a summary of every answer. Tap any row to change just that one; it saves immediately.
4. **Planner** (`roamly.html`) ✅: straight to the home screen.

**One design system ✅** (`webui/roamly.css`), shared by the planner, My taste and sign-in pages, with the landing page using the same palette:
- cream background, deep-navy text, violet→magenta gradient only on primary actions and the hero
- Plus Jakarta Sans
- rounded cards and pills, soft layered shadows, and a compass-star logo
- a dusk-city hero drawn in code, so it needs no image download

**App layout ✅:**
- **Phones:** bottom navigation (Home · What's on · Saved · My taste). Settings open as a bottom sheet.
- **Tablets:** an icon sidebar. Settings open as a centred dialog.
- **Desktop:** a full sidebar, with the settings as an **always-open panel on the right**.

### 4.2 The planner screen
```
┌──────────────────────────────────────┐
│ ✦ Roamly                         (A) │  ← dusk skyline hero; (A) = account menu
│ Good evening, Alex ✨                 │
│ What are we up to?                   │
│ ┌ PLANNING AROUND ───────── Adjust ┐ │
│ │ [📍 Near you][◎ 7 km][$100]      │ │  ← tap any chip to change it
│ │ [Tonight][Date][Chill][Transit]  │ │
│ │ 📍 Using your approximate location│ │
│ └──────────────────────────────────┘ │
│ ┌ ✦ Plan my night ─────────────── › ┐ │  ← the primary action
│ [What's on now][Surprise me][Pick my spots]
│ ┌ ✦ Ask Roamly…                 ↑ ┐ │
│ [🍣 Cheap sushi date][🎶 What's on tonight?]…  ← tap to ask
│ Pick up where you left off ›         │
│ Your saved plans  ▸ ▸ ▸              │
├──────────────────────────────────────┤
│  Home · What's on · Saved · My taste │
└──────────────────────────────────────┘
```

**Home screen ✅:** a greeting, the Planning-around card, and one big **Plan my night** with three smaller choices under it. There's an "Ask Roamly" box with example questions to tap, and "Pick up where you left off" and your saved plans when you have them. Nothing to fill in first.

**Chat screen ✅:** any action or typed message opens the conversation, with a back arrow to Home, **Start over**, and the Planning-around bar across the top.

**Planning around bar ✅:** the settings in play, shown as chips. Tap one, or **Adjust**, to open the **settings sheet**. It's a bottom sheet on phones and a dialog on desktop, with every control grouped in one place:

| Section | Control | Range and behaviour |
|---|---|---|
| 📍 **Where** | **Use my live location** switch, plus a box to type a place | The switch is off until you turn it on. It says what it's doing in plain words ("Using your approximate current location" / "Your live location isn't being used"). A typed place takes priority over live location. |
| 📏 **How far** | Slider, plus Walk / Transit / Car shortcuts | 1–20 km. Shortcuts: walk ~2 km, transit ~7 km, car ~15 km. The starting value comes from your survey. |
| 💸 **Budget** | $50 / $100 / $200 / $500, plus any amount | $0–$10,000 total for the group, with "≈ $X each" shown. With a taste profile, changing the group size keeps your usual per-person spend, unless you set the budget yourself. |
| 🕖 **When** | Full day · Afternoon · Evening · Late night | Sets how the plan's stops are arranged across the day |
| 👥 **Who** | Solo · Date · Friends · Family, plus a − / + people counter | 1–20 people |
| ✨ **Mood** | Chill · Romantic · Adventurous · Fancy · Night out | Shapes activities and price level |
| 🚶 **Getting there** | Walk · Transit · Car | Walking keeps stops close to each other |
| 🔎 **Looking for** | Free text, e.g. "sushi, karaoke" | Searched for first; shown as a chip while it's set |

Changes apply as you tap. **Reset to my defaults** goes back to your profile's values. If an action was waiting for a place (Plan my night with no location), it carries on by itself once you set one.

**Typing ✅:** say it however you like. Roamly works out the budget ("$60", "50 each"), the vibe, who's coming, the time of day, transport, distance ("within 3 km", "under 2 miles"), what you're looking for ("sushi"), places to include by name, and things to avoid ("no clubs").
- **One set of settings.** A typed message also sends the bar's current settings. What you say wins, the bar fills in what you didn't say, and your profile fills in the rest. The chat, the bar and the planner never disagree.
- **"Roamly heard"** appears under your message, showing exactly which settings it changed (e.g. `3 km · $60 · Walking`). Tap one to correct it. The changed chips in the bar flash once.

**Waiting ✅:** short status lines describing what the planner is doing ("Checking what's nearby…", "Finding places that fit…"), with a plan-shaped placeholder. After 15 seconds, a "taking longer than usual" note appears. It never shows a fake progress bar.

**When something fails ✅:**
- A plain explanation, never blaming you. If the venue search itself is slow, it says so instead of suggesting a bigger budget.
- **Try again** re-runs the same request, and **Adjust settings** opens the sheet. Your settings are kept, so nothing needs retyping.
- If a place is needed first, it opens the **Where** section with "Tell me where first".

### 4.3 A plan
✅ Each plan card is laid out like an itinerary:
- A title ("Tonight's plan · 😌 Chill date") and quick facts: total cost, number of stops, and route length.
- A **mini route map** of the numbered stops (OpenStreetMap).
- A **timeline** of stops. Each shows its category, name, cost per person, address, a photo when the source has one, and **Directions** / **Website** links. A suggested time appears when the AI narrator is available.
- **Why this fits**: up to four lines, each checked against the plan's own numbers:
  - within budget, or honestly "about $16 over"
  - how close together the stops are
  - that what you asked for is included
  - the weather checked
  - that your taste profile was used

✅ **After a plan:**
- **Save plan** and **Try another** (new places, same settings).
- **"Make it more…"** chips: Cheaper · Closer · Fancier · More relaxed · More adventurous · More romantic · More social · Surprise me. Each one changes the actual settings (the bar flashes) and re-plans from them, without an AI call. If the same places still win, Roamly says so instead of pretending.
- Up to two nearby events offered as "want me to add it?"

🔜 **Planned additions:**
- **"Why this fits you"** per stop (e.g. "vegan-friendly · quiet, like you prefer"), worked out from the actual ranking.
- **Swap one stop** instead of re-rolling the whole plan.
- 👍 / 👎 / **"Been there"** on each stop, feeding the learning loop ([§11](#11-roadmap)).
- **Walking minutes** between stops.

### 4.4 Navigation and account
- ✅ **What's on:** Right now / Tonight / This weekend tabs, filters built from what's actually listed (Free, Music, Festival…), and **Plan around it** on every event.
- 🟡 **Saved:** a visual collection of cards (stops, cost, place, date). Tap to reopen; deleting offers **Undo**. Kept in this browser only; 🔜 moving to your account.
- ✅ **My taste:** the profile summary ([§5](#5-the-taste-profile-survey)).
- ✅ **Account menu:** Start over and **Sign out**.
- 🔜 **Account page:** edit name and email, change password, **delete account**. The API already supports these; the screen doesn't exist yet.

---

## 5. The taste profile (survey)

✅ 14 tap-only questions in 4 sections, taking about 2 minutes. Each answer changes something specific about your plans:

| # | Question | What it changes |
|---|---|---|
| **You & your people** |||
| 1 | Who do you usually go out with? | Default group type and number of people |
| 2 | When are you at your best? | Default time of day |
| 3 | How do you usually get around? | Default transport and starting radius |
| 4 | How far will you go for something great? | Search radius |
| **Your style** |||
| 5 | Your energy on a free day? | Default mood; matches venue energy (calm ↔ intense) |
| 6 | What kind of places pull you in? (quiet ↔ buzzing) | How much popular, crowded places are favoured |
| 7 | Old favourites or something new? | More variety between plans |
| 8 | How packed should a day out be? | Number of stops (2–3, about 4, or 5+) |
| **Food** |||
| 9 | Dietary needs | Restaurant searches (vegan, halal, gluten-free…) |
| 10 | Cuisines you love | A small ranking boost (it never forces the same cuisine every time) |
| 11 | Usual spend per person on a meal | Default budget and price level |
| **Fun** |||
| 12 | Things you love doing | A small ranking boost for matching places |
| 13 | Hard nopes | Never suggested |
| 14 | Drinks? | "No alcohol" removes bars and turns the evening stop into shows, games or music |

Taste is stored on your account and deleted along with it.

---

## 6. How a plan is made (simplified)

1. **Understand the request.**
   - Tapped settings go straight to the planner.
   - Typed requests are read by an AI model (Groq's free tier), with a keyword reader as backup.
   - Your profile fills in anything not said.
2. **Decide the stops.** Mood and time of day give a sequence, such as activity → dinner → drinks → dessert. The number of stops follows your pace.
   - A rainy or cold day swaps outdoor stops for indoor ones.
   - "No alcohol" and family plans skip bars.
3. **Find candidates** for every stop at once, within your radius ([§9](#9-data-sources)).
4. **Rank them:**
   - rating, distance, value for the budget and mood
   - popularity, weighted by your crowd preference
   - energy match for activities
   - your loves (a small boost) and your "nevers" and avoids (filtered out)
5. **Fit the budget:** if the estimate runs over, drop the least essential stop first (dessert before the activity).
6. **Add what's on nearby:** live events in the next 24 hours.
7. **Describe the plan:** the AI writes a short intro and suggested times. If it's slow, a plain template is used.

**Always finishes:** a plan comes back within **25 seconds**. If a venue source is slow, the plan comes back with what's ready and says what's missing. The slow searches keep going in the background, so **Try again** is quick.

---

## 7. Principles

1. **Honest over impressive.**
   - No invented statistics, venues or events.
   - Costs are estimates and shown that way.
   - 🔜 "Open now" only when opening hours are actually known.
   - When the data source is struggling, Roamly says so.
2. **Your location is yours.**
   - Live location is **off until you turn it on**, with plain-language status.
   - Roamly never says "I know where you are".
   - A typed place always works instead.
3. **Controls, not questionnaires.** You can start immediately. The survey is optional, and questions are asked only when truly needed, one at a time with tappable answers.
4. **Fast and light.**
   - No constantly animated backgrounds or live blur effects (these made the site lag).
   - Plans have a time limit.
   - Tapped settings skip the AI entirely.
5. **Accessible.**
   - Text contrast meets WCAG AA; the planner passes an automated check.
   - Inputs and controls have labels.
   - Tap targets are large (40–44 px).
   - Escape closes panels, and keyboard focus stays inside the open settings sheet.
   - New replies are announced to screen readers.
   - Every screen passes an automated axe (WCAG 2 AA) check on phone and desktop.
   - 🔜 A full manual keyboard and screen-reader pass.
6. **Free to run.** No paid APIs; free tiers and open data only ([§9](#9-data-sources)).
7. **Safe by default.**
   - Links from third-party event listings must be `http(s)`.
   - Password-reset tokens are only ever emailed, never shown.
   - Unused API features are switched off.

---

## 8. What's on now: live events

✅ **How to ask:** tap **🔴 What's on now**, or ask "what's happening right now", "events tonight", "any festivals this weekend".

✅ **Time windows**, in your time zone:

| Window | Covers |
|---|---|
| **Right now** | Events that started up to 2 h ago or start in the next 4 h |
| **Tonight** | Now until 3 am |
| **This weekend** | Friday 5 pm to Sunday 11:59 pm (from now, if it's already the weekend) |

✅ **Event cards** show:
- 🔴 **On now** / "Starts in 40 min" / "8:30 PM"
- a **Free** badge or a price
- venue and distance, and where the listing came from
- buttons: **➕ Plan around it** (builds a plan centred on the event, with the event included), **📍 Directions**, **🎫 Tickets / ℹ️ Details**

When nothing is listed, it says so and offers a wider radius or a plan instead.

**Sources:**
| Source | Covers | Status |
|---|---|---|
| Ticketmaster | Ticketed concerts, sports, comedy, theatre | ✅ Working with the production key |
| City of Toronto *Festivals & Events* open data | Free public festivals, square programming, film-festival events | 🟡 Built, but the city's feed currently refuses all requests (reported as broken on the portal). It starts contributing automatically once restored. |
| Web search + AI extraction | Free local events no other source lists | 🔜 Under consideration. It needs a search-API key and strict rules: a source link on every event, "check details" wording, never shown as confirmed. |

Eventbrite no longer offers public event search, and Meetup's API requires a paid plan, so neither is used.

---

## 9. Data sources

| Purpose | Source | Cost | Notes |
|---|---|---|---|
| Venues (free fallback) | OpenStreetMap via the public **Overpass** API | Free | Reliable venue data, but the public servers are often overloaded, so searches are combined, cached for 7 days and time-limited. |
| Venues (main free source) | **Geoapify** Places | Free tier (3,000 requests/day) | 🔜 Chosen; waiting on an API key. Adds reliable 1–20 km searches, address search, and opening hours for "open now". |
| Venues (keyed) | Yelp, Foursquare | ⚠️ Paid beyond trials | Keys exist in production but are deliberately not paid for. Without billing they just refuse requests and the planner falls back. |
| Understanding typed requests, writing plan descriptions | Groq (LLM) | Free tier | Keyword fallback when unavailable |
| Place-name search | OpenStreetMap Nominatim | Free | Limited to 1 request per second |
| Weather | Open-Meteo | Free for non-commercial use | Commercial launch needs its paid plan |
| Events | Ticketmaster; City of Toronto open data | Free | See [§8](#8-whats-on-now-live-events) |
| Map tiles | OpenStreetMap | Free | Must show "© OpenStreetMap" |
| Email (password reset) | Brevo | Free tier | 🔜 Waiting on account setup. Until then, reset emails aren't sent. |

**Hosting:**
- Website: Vercel (static)
- API: Render (free plan, kept awake by a ping every 5 minutes). No background worker.
- Database: Postgres on Render. ⚠️ The free database **expires 2026-10-25**; move to a paid database with backups before real users arrive.

---

## 10. Not part of Roamly (removed or switched off)

- **The hidden-gems map app:** removed entirely, including the gem catalog and "hidden gem" ranking.
- **Travel logging** (trips, friends, feed, leaderboards, challenges, badges, share cards): these came from an earlier project ("TrekRank"). Removed: migration `0009` drops their tables and the travel columns on `users`, and their code is gone (see `CLEANUP.md`).

---

## 11. Roadmap

**Now (built):**
- the chat + controls planner
- the Planning around bar and settings sheet, with custom radius, budget and group size
- the optional taste survey
- live events with Ticketmaster
- honest waiting and Retry
- time-limited plans
- security fixes (two account-takeover holes closed in code)
- accessibility fixes on the planner

**Next:**
1. Geoapify as the main free venue source, adding address search and open-now.
2. The account page (edit details, password, sign out, delete account) and working password-reset emails (Brevo).
3. "Why this fits you", "Make it more…" chips, and swap one stop.
4. Saved plans stored on the account.
5. A shared stylesheet, a light-themed sign-in page, and the 🧭 compass app icon, favicon and share image.

**Later:**
- Intent modes: 🍽️ Eat now · 🥱 I'm bored · ✈️ Trip
- Learning from 👍 / 👎 / "been there", plus the occasional one-tap question to keep taste fresh
- More free event sources for free local events
- An installable app (PWA: works offline for saved plans, can be added to the home screen), then app-store packaging

**Before a public launch:**
- **Legal:** Terms of Service and Privacy Policy pages (the sign-in page already links to them).
- **Store disclosures:** location, email, taste profile, and data sent to third parties. Label AI-generated text.
- **Account deletion:** in-app deletion, required by both app stores.
- **Data-provider rules:** OpenStreetMap, Ticketmaster and others require credits and have display rules.
- **Age rating:** suggesting bars affects the app's age rating.
- **Security:** upgrade the dependencies with known vulnerabilities, lock down which sites can call the API, hide internal pages, tighten rate limits, require email verification and stronger passwords, and add security headers.
- **Reliability:** a paid database with backups, automated tests on every push, and error monitoring.

---

## 12. Known limitations and open questions

- **Venue quality depends on the data source.** On OpenStreetMap alone, you can get odd picks, such as a coffee shop as a "fancy dinner". Geoapify should help.
- **Toronto's festival feed is down.** Worth reporting to Toronto Open Data (opendata@toronto.ca).
- **"When" is four time slots,** not an exact start time. An exact time needs a planner change.
- **The name "Roamly" appears to be used by another travel product** (roamly.io). Check for trademark conflicts before publishing.
- **The travel-logging features** from the earlier project: bring them back with real screens, or remove them for good?
