"""Taste profile: the onboarding survey, its validation, and what it means for
the planner.

The survey captures *who you are* (stable traits: crew, energy, food rules,
spend, loves and nopes) so each planning request only has to carry *what you
want right now*. Raw answers are stored as-is (``user_profiles.answers``); how
they translate into planner settings lives only here, so tuning the mapping
never needs a migration.
"""
from __future__ import annotations

SURVEY_VERSION = 1

# Multi-select chips for loves / hard nopes. `match` are lowercase fragments of
# real venue category titles/names (Yelp & Foursquare titles, OSM subtypes), so a
# nope genuinely filters venues and a love genuinely nudges ranking.
_FUN = [
    ("live_music", "🎸 Live music",          ["music venue", "jazz", "live music", "concert"]),
    ("games",      "🕹️ Games & arcades",     ["arcade", "bowling", "escape", "laser", "kart", "mini golf",
                                              "minigolf", "miniature", "billiard", "pool hall", "karaoke"]),
    ("museums",    "🏛️ Museums & galleries", ["museum", "galler"]),
    ("nature",     "🌿 Parks & nature",       ["park", "garden", "beach", "trail", "nature", "viewpoint"]),
    ("active",     "🧗 Sports & active",      ["climbing", "sport", "golf", "skating", "trampoline", "kayak"]),
    ("comedy",     "😂 Comedy",               ["comedy"]),
    ("theatre",    "🎭 Theatre & shows",      ["theater", "theatre", "cinema", "performing art"]),
    ("wellness",   "💆 Spas & wellness",      ["day spa", "massage", "wellness"]),
    ("markets",    "🧺 Markets",              ["market"]),
    ("views",      "🌇 Rooftops & views",     ["rooftop", "observ", "lookout", "viewpoint"]),
    ("nightlife",  "🪩 Nightlife & clubs",    ["dance club", "nightclub", "night club", "nightlife", "lounge"]),
]
_CUISINES = [
    ("italian",       "🍝 Italian",          ["italian", "pasta"]),
    ("japanese",      "🍣 Japanese",         ["japanese", "sushi", "ramen", "izakaya"]),
    ("korean",        "🥘 Korean",           ["korean"]),
    ("chinese",       "🥟 Chinese",          ["chinese", "dim sum", "dimsum", "szechuan", "cantonese"]),
    ("thai",          "🍜 Thai",             ["thai"]),
    ("vietnamese",    "🍲 Vietnamese",       ["vietnamese", "pho"]),
    ("indian",        "🍛 Indian",           ["indian", "pakistani"]),
    ("mexican",       "🌮 Mexican",          ["mexican", "taco"]),
    ("mediterranean", "🥙 Mediterranean",    ["mediterranean", "greek", "lebanese", "middle eastern", "turkish"]),
    ("burgers_bbq",   "🍔 Burgers & BBQ",    ["burger", "bbq", "barbeque", "smokehouse"]),
    ("pizza",         "🍕 Pizza",            ["pizza"]),
    ("seafood",       "🦞 Seafood",          ["seafood", "fish"]),
    ("brunch",        "🥞 Brunch & cafés",   ["brunch", "breakfast", "cafe", "coffee"]),
]
_DIETS = [
    ("vegetarian", "🥗 Vegetarian", "vegetarian"),
    ("vegan",      "🌱 Vegan",      "vegan"),
    ("halal",      "☪️ Halal",      "halal"),
    ("kosher",     "✡️ Kosher",     "kosher"),
    ("gluten_free", "🌾 Gluten-free", "gluten-free"),
    ("dairy_free", "🥛 Dairy-free", "dairy-free"),
    ("nut_free",   "🥜 Nut allergy", "nut-free"),
]


def _opts(pairs):
    return [{"id": i, "label": label} for i, label in pairs]


# The survey itself — the front end renders exactly this (GET /users/me/profile/questions).
QUESTIONS = [
    # ---- You & your people ----
    {"id": "crew", "section": "You & your people", "type": "single",
     "q": "Who do you usually go out with?",
     "options": _opts([("solo", "🧍 Just me"), ("partner", "💘 My partner"),
                       ("friends", "🎉 Friends"), ("family", "👨‍👩‍👧 Family")])},
    {"id": "time", "section": "You & your people", "type": "single",
     "q": "When are you at your best?",
     "options": _opts([("morning", "☀️ Mornings"), ("afternoon", "🌤️ Afternoons"),
                       ("evening", "🌆 Evenings"), ("night", "🌙 Late nights")])},
    {"id": "transport", "section": "You & your people", "type": "single",
     "q": "How do you usually get around?",
     "options": _opts([("walk", "🚶 Walking"), ("transit", "🚇 Transit"), ("car", "🚗 Car")])},
    {"id": "distance", "section": "You & your people", "type": "single",
     "q": "How far will you go for something great?",
     "options": _opts([("near", "📍 ~10 min"), ("medium", "🛣️ ~20 min"), ("far", "🗺️ 30+ min")])},
    # ---- Your style ----
    {"id": "energy", "section": "Your style", "type": "single",
     "q": "Your energy on a free day?",
     "options": _opts([("recharge", "🛋️ Recharge & relax"), ("balanced", "⚖️ A bit of both"),
                       ("gogogo", "🚀 Go-go-go")])},
    {"id": "crowds", "section": "Your style", "type": "single",
     "q": "What kind of places pull you in?",
     "options": _opts([("quiet", "🕯️ Quiet & cozy"), ("depends", "🤷 Depends on my mood"),
                       ("buzzing", "🎉 Buzzing & busy")])},
    {"id": "novelty", "section": "Your style", "type": "single",
     "q": "Old favourites or something new?",
     "options": _opts([("favourites", "❤️ My favourites"), ("mix", "🔀 A mix"),
                       ("new", "✨ Always something new")])},
    {"id": "pace", "section": "Your style", "type": "single",
     "q": "How packed should a day out be?",
     "options": _opts([("relaxed", "🐢 2–3 stops"), ("steady", "🚶 About 4"),
                       ("packed", "⚡ 5+ stops")])},
    # ---- Food ----
    {"id": "diet", "section": "Food", "type": "multi", "optional": True,
     "q": "Any dietary needs?", "hint": "Pick all that apply — or skip if none",
     "options": _opts([(i, label) for i, label, _ in _DIETS])},
    {"id": "cuisines", "section": "Food", "type": "multi",
     "q": "Cuisines you love", "hint": "Pick as many as you like",
     "options": _opts([(i, label) for i, label, _ in _CUISINES])},
    {"id": "spend", "section": "Food", "type": "single",
     "q": "What do you usually spend per person on a meal?",
     "options": _opts([("1", "$ · under $20"), ("2", "$$ · $20–40"),
                       ("3", "$$$ · $40–80"), ("4", "$$$$ · $80+")])},
    # ---- Fun ----
    {"id": "loves", "section": "Fun", "type": "multi",
     "q": "Things you love doing", "hint": "Pick as many as you like",
     "options": _opts([(i, label) for i, label, _ in _FUN])},
    {"id": "nopes", "section": "Fun", "type": "multi", "optional": True,
     "q": "Hard nopes — never suggest these", "hint": "Optional",
     "options": _opts([(i, label) for i, label, _ in _FUN])},
    {"id": "drinks", "section": "Fun", "type": "single",
     "q": "Drinks?",
     "options": _opts([("love", "🍸 Love a good bar"), ("sometimes", "🥂 Sometimes"),
                       ("none", "🚫 No alcohol")])},
]
_BY_ID = {q["id"]: q for q in QUESTIONS}
REQUIRED = [q["id"] for q in QUESTIONS if not q.get("optional")]

_FUN_MATCH = {i: m for i, _, m in _FUN}
_CUISINE_MATCH = {i: m for i, _, m in _CUISINES}
_DIET_TERM = {i: t for i, _, t in _DIETS}


def validate(answers: dict) -> dict:
    """Keep only known questions with valid option ids. Partial answers are fine
    (the survey autosaves as you go); unknown keys/options are dropped rather than
    rejected so an older client can't brick the save."""
    clean = {}
    for qid, val in (answers or {}).items():
        q = _BY_ID.get(qid)
        if not q:
            continue
        valid = {o["id"] for o in q["options"]}
        if q["type"] == "single":
            if isinstance(val, str) and val in valid:
                clean[qid] = val
        else:
            if isinstance(val, list):
                clean[qid] = [v for v in dict.fromkeys(val) if isinstance(v, str) and v in valid]
    return clean


def is_complete(answers: dict) -> bool:
    return all(qid in answers for qid in REQUIRED)


# ---------------------------------------------------------------- planner mapping

_GROUP = {"solo": ("solo", 1), "partner": ("date", 2), "friends": ("friends", 4), "family": ("family", 4)}
# Rough per-person budget for a whole outing (meal + activity + a finish), by meal-spend tier.
_OUTING_PER_PERSON = {"1": 40, "2": 70, "3": 120, "4": 200}
_ENERGY_TARGET = {"recharge": 1.8, "balanced": 3.0, "gogogo": 4.3}    # on experience.arousal's ~1–5 scale
_CROWD = {"quiet": -1.0, "depends": 0.0, "buzzing": 1.0}
_RADIUS_SCALE = {"near": 0.6, "medium": 1.0, "far": 1.4}
_TARGET_STOPS = {"relaxed": 3, "steady": 4, "packed": 5}
_TRANSPORT_KM = {"walk": 2.0, "transit": 7.0, "car": 15.0}   # matches the slider's presets


def _default_vibe(a: dict) -> str:
    energy, crew, spend = a.get("energy"), a.get("crew"), a.get("spend")
    no_alcohol = a.get("drinks") == "none"
    if spend == "4":
        return "extravagant"
    if energy == "gogogo":
        return "night_out" if crew == "friends" and not no_alcohol else "adventurous"
    if energy == "recharge":
        return "chill"
    if crew == "partner":
        return "romantic"
    if crew == "friends" and not no_alcohol:
        return "night_out"
    return "chill"


def _tokens(ids, table):
    out = []
    for i in ids or []:
        out.extend(table.get(i, []))
    return ", ".join(dict.fromkeys(out))


def defaults_for(answers: dict | None) -> dict:
    """Planner defaults implied by the profile. Only keys the profile actually
    answers are returned, so a partial/missing profile changes nothing."""
    a = answers or {}
    d: dict = {}
    if a.get("crew") in _GROUP:
        d["group_type"], d["party_size"] = _GROUP[a["crew"]]
    if a.get("time"):
        d["time_of_day"] = a["time"]
    if a.get("transport"):
        d["transport"] = a["transport"]
    if a.get("distance") in _RADIUS_SCALE:
        d["radius_scale"] = _RADIUS_SCALE[a["distance"]]
    if a.get("transport") or a.get("distance"):
        # A concrete starting value for the planner's radius slider (1-20 km):
        # how far that mode of transport usually reaches × how far they'll go.
        base = _TRANSPORT_KM.get(a.get("transport"), 7.0)
        d["radius_km"] = round(max(1.0, min(20.0, base * _RADIUS_SCALE.get(a.get("distance"), 1.0))), 1)
    if a.get("energy") in _ENERGY_TARGET:
        d["energy"] = _ENERGY_TARGET[a["energy"]]
    if a.get("crowds") in _CROWD:
        d["crowd_pref"] = _CROWD[a["crowds"]]
    if a.get("novelty") == "new":
        d["vary"] = True                     # spread picks across the strong top slice
    if a.get("pace") in _TARGET_STOPS:
        d["target_stops"] = _TARGET_STOPS[a["pace"]]
    diets = [_DIET_TERM[i] for i in a.get("diet") or [] if i in _DIET_TERM]
    if diets:
        d["dietary"] = ", ".join(diets)
    nopes = set(a.get("nopes") or [])
    loves = [x for x in a.get("loves") or [] if x not in nopes]   # a hard no beats a love
    likes = ", ".join(filter(None, [_tokens(a.get("cuisines"), _CUISINE_MATCH),
                                    _tokens(loves, _FUN_MATCH)]))
    if likes:
        d["likes"] = likes
    avoid = _tokens(a.get("nopes"), _FUN_MATCH)
    if avoid:
        d["avoid"] = avoid
    if a.get("drinks") == "none":
        d["no_alcohol"] = True
    if a.get("spend") in _OUTING_PER_PERSON:
        # per-person is what the planner re-scales when a request names a different crew
        d["budget_per_person"] = float(_OUTING_PER_PERSON[a["spend"]])
        d["budget"] = d["budget_per_person"] * d.get("party_size", 2)
    if a.get("energy") or a.get("crew") or a.get("spend"):
        d["vibe"] = _default_vibe(a)
    return d


# ---------------------------------------------------------------- result screen

def archetype(answers: dict) -> dict:
    """A fun, shareable one-liner from energy × crowds plus a food lean."""
    a = answers or {}
    names = {
        ("recharge", "quiet"): ("🌙", "Cozy"), ("recharge", "depends"): ("🍃", "Easygoing"),
        ("recharge", "buzzing"): ("☕", "Laid-back Social"),
        ("balanced", "quiet"): ("🧭", "Curious"), ("balanced", "depends"): ("⚖️", "All-rounder"),
        ("balanced", "buzzing"): ("✨", "Social"),
        ("gogogo", "quiet"): ("🧗", "Adventurous"), ("gogogo", "depends"): ("🚀", "High-energy"),
        ("gogogo", "buzzing"): ("🔥", "Life-of-the-party"),
    }
    emoji, first = names.get((a.get("energy"), a.get("crowds")), ("🧭", "Curious"))
    loves = a.get("loves") or []
    if len(a.get("cuisines") or []) >= 3 or "markets" in loves:
        middle = "Foodie"
    elif "nature" in loves or "active" in loves:
        middle = "Outdoorsy"
    elif "museums" in loves or "theatre" in loves:
        middle = "Culture"
    elif "nightlife" in loves or "live_music" in loves:
        middle = "Night-owl"
    else:
        middle = "City"
    last = "Explorer" if a.get("novelty") == "new" else ("Regular" if a.get("novelty") == "favourites" else "Wanderer")
    return {"emoji": emoji, "title": f"{first} {middle} {last}"}


def summary(answers: dict) -> list[str]:
    """Short chips describing the profile, for the result screen / account page."""
    a = answers or {}
    label = lambda qid: next((o["label"] for o in _BY_ID[qid]["options"] if o["id"] == a.get(qid)), None)
    labels_of = lambda qid: [o["label"] for o in _BY_ID[qid]["options"] if o["id"] in (a.get(qid) or [])]
    chips = [c for c in (label("crew"), label("energy"), label("crowds"), label("transport"), label("spend")) if c]
    nope_labels = labels_of("nopes")
    chips += labels_of("diet") + labels_of("cuisines")[:3]
    chips += [x for x in labels_of("loves") if x not in nope_labels][:3]
    chips += [f"no {x.split(' ', 1)[-1].lower()}" for x in nope_labels[:3]]
    if a.get("drinks") == "none":
        chips.append("🚫 No alcohol")
    return chips
