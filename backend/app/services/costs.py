"""What a stop costs per person, roughly, and how a budget is shared across stops.

The free venue data has no prices, so the estimate comes from, in order:
  1. a price tier from a venue source that has one (Yelp "$$"),
  2. the admission price of a well-known attraction (KNOWN_PRICES),
  3. the kind of place ("sushi restaurant" → ~$45 at dinner, "escape room" → ~$35),
  4. a default for the kind of stop.
Figures are approximate Toronto prices in dollars per person (2026): admission for
attractions, a typical meal with a drink for restaurants, a couple of drinks for bars.
They steer choices and the budget check; the page labels them "~$".
"""
import re

PRICE_COST = {"$": 15, "$$": 30, "$$$": 65, "$$$$": 110}

# Typical cost of each kind of stop (the fallback when nothing better is known).
TYPICAL = {"scenic": 0, "cafe": 8, "dessert": 9, "activity": 30, "lunch": 25, "dinner": 40,
           "leisure": 28, "drinks": 28, "event": 40}
# The cheapest reasonable version of each (a free gallery, a casual dinner): when even
# these don't fit the budget, the plan has fewer stops instead of cheaper ones.
MODEST = {"scenic": 0, "cafe": 6, "dessert": 7, "activity": 10, "lunch": 15, "dinner": 25,
          "leisure": 18, "drinks": 18, "event": 30}
# How the budget is shared: dinner gets the biggest part.
SHARE = {"scenic": 0, "cafe": 7, "dessert": 8, "activity": 25, "lunch": 30, "dinner": 50,
         "leisure": 22, "drinks": 22, "event": 40}
DINNER_FLOOR = 15          # below this it's a snack (hot dog, bagel), not dinner
LUNCH_SHARE = 0.7          # lunch costs ~70% of the same restaurant at dinner

# Adult admission at well-known attractions, matched by name. Update when prices change.
KNOWN_PRICES = {
    "cntower": 45, "ripleysaquarium": 45, "royalontariomuseum": 26, "artgalleryofontario": 30,
    "casaloma": 40, "torontozoo": 40, "hockeyhalloffame": 25, "agakhanmuseum": 20,
    "batashoemuseum": 14, "gardinermuseum": 15, "canadaswonderland": 70, "centrevilleamusementpark": 40,
    "blackcreekpioneervillage": 20, "fortyork": 15, "torontoislands": 9, "powerplant": 0,
    "textilemuseumofcanada": 15, "museumofcontemporaryart": 10, "torontobotanicalgarden": 0,
}

# Kind of place → price, most specific first (the first keyword found wins).
_BY_KIND = [
    # meals
    ("fine dining", 120), ("steakhouse", 80), ("french", 65), ("brasserie", 55), ("seafood", 55),
    ("tapas", 50), ("spanish", 50), ("bistro", 50), ("european", 50), ("korean barbecue", 45),
    ("sushi", 45), ("asian fusion", 45), ("hot pot", 42), ("japanese", 42), ("italian", 42),
    ("portuguese", 42), ("mediterranean", 42), ("gastropub", 40), ("greek", 38), ("theme restaurant", 38),
    ("canadian", 38), ("buffet", 35), ("american", 35), ("barbecue", 35), ("turkish", 35),
    ("dim sum", 32), ("indian", 32), ("thai", 32), ("korean", 32), ("latin american", 32),
    ("mexican", 32), ("lebanese", 32), ("chinese", 30), ("middle eastern", 30), ("vegan", 30),
    ("vegetarian", 30), ("ethiopian", 28), ("pakistani", 28), ("brunch", 26), ("filipino", 25),
    ("caribbean", 24), ("ramen", 24), ("breakfast", 24), ("vietnamese", 22), ("noodle", 22),
    ("pizza", 22), ("diner", 22), ("delicatessen", 22), ("jamaican", 22), ("chicken wings", 22),
    ("burger", 20), ("dumpling", 20), ("pho", 20), ("taco", 18), ("fried chicken", 18),
    ("burrito", 16), ("shawarma", 15), ("cafeteria", 15), ("fast food", 14), ("food truck", 14),
    ("sandwich", 14), ("food stand", 12), ("hot dog", 10), ("bagel", 10),
    # drinks & nights out
    ("wine bar", 40), ("speakeasy", 38), ("cocktail bar", 35), ("lounge", 35), ("hookah", 35),
    ("jazz", 32), ("nightclub", 30), ("dance club", 30), ("music venue", 30), ("comedy", 28),
    ("sports bar", 28), ("pub", 26), ("brewery", 26), ("beer", 26), ("karaoke", 22), ("billiard", 15),
    ("bar", 28),
    # cafés & sweets
    ("creperie", 14), ("patisserie", 12), ("chocolatier", 12), ("dessert", 11), ("bakery", 9),
    ("cafe", 9), ("bubble tea", 8), ("tea room", 8), ("ice cream", 8), ("gelato", 8),
    ("frozen yogurt", 8), ("coffee", 7), ("cupcake", 7), ("donut", 6),
    # things to do
    ("opera", 90), ("ballet", 90), ("amusement park", 60), ("theme park", 60), ("concert hall", 60),
    ("circus", 50), ("observation tower", 45), ("water park", 45), ("paintball", 45), ("aquarium", 40),
    ("castle", 40), ("zoo", 35), ("escape", 35), ("axe", 35), ("go kart", 35), ("kart", 35),
    ("virtual reality", 35), ("haunted house", 30), ("archery", 30), ("science museum", 25),
    ("laser tag", 25), ("climbing", 25), ("arcade", 25), ("art museum", 22), ("trampoline", 22),
    ("driving range", 20), ("bowling", 20), ("mini golf", 18), ("movie theater", 17), ("cinema", 17),
    ("drive-in", 15), ("planetarium", 15), ("history museum", 15), ("fort", 15), ("skating", 12),
    ("performing arts", 70), ("theatre", 65), ("theater", 65), ("museum", 18), ("market", 10),
    ("art gallery", 8), ("gallery", 8), ("observatory", 5),
]
_KIND_RE = [(re.compile(rf"\b{re.escape(k)}(?:e?s)?\b"), k, p) for k, p in _BY_KIND]
_MEALS = ("dinner", "lunch")


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower().replace("&", "and"))


def known_price(name: str) -> int | None:
    key = _key(name)
    return next((p for k, p in KNOWN_PRICES.items() if k in key), None)


def by_kind(categories) -> int | None:
    """Price for the most specific category we have a figure for."""
    for cat in categories or []:
        text = str(cat).lower().replace("_", " ")
        for rx, _, price in _KIND_RE:
            if rx.search(text):
                return price
    return None


def estimate(c: dict, slot_key: str) -> int:
    """Rough cost per person of this place as this kind of stop."""
    if c.get("price") in PRICE_COST:
        return PRICE_COST[c["price"]]
    if slot_key in ("activity", "scenic"):          # not "360 Restaurant at the CN Tower"
        known = known_price(c.get("name") or "")
        if known is not None:
            return known
    if slot_key == "scenic":
        return 0
    price = by_kind(c.get("categories"))
    if price is None:
        return TYPICAL.get(slot_key, 30)
    if slot_key == "lunch":
        price = price * LUNCH_SHARE
    return int(round(price))


def allowances(slots, budget, party_size) -> dict:
    """Each stop's share of the budget per person (a park gets nothing, dinner the
    most). {} = no budget."""
    if not budget or budget <= 0:
        return {}
    pp = budget / max(1, party_size or 1)
    weight = sum(SHARE.get(s, 25) for s in slots)
    if weight <= 0:
        return {}
    return {s: pp * SHARE.get(s, 25) / weight for s in slots}


OVER_WEIGHT = 3.0    # points lost per 100% over a stop's share (capped at 200%)
SPEND_WEIGHT = 1.0   # with money to spend, the nicer (pricier) place ranks a little higher
FANCY_SPEND = 2.5    # …and clearly higher when they asked for fancier


SNACK_DINNER = 2.0   # points lost by a snack place as dinner when there's money for a meal


def fit(est: int, allowance: float | None, slot_key: str | None = None, fancy: bool = False) -> float:
    """Score adjustment for a place costing `est` against its share of the budget."""
    if allowance is None:
        return 0.0
    room = max(allowance, 10.0)
    if est > allowance:
        return -OVER_WEIGHT * min(2.0, (est - allowance) / room)
    score = (FANCY_SPEND if fancy else SPEND_WEIGHT) * est / room
    if slot_key == "dinner" and est < DINNER_FLOOR <= allowance:
        score -= SNACK_DINNER
    return score


def fewer_stops(slots, budget, party_size, keep=lambda s: False) -> list:
    """With a tight budget, plan fewer stops rather than cheapen every one: drop
    optional stops (dessert, café, drinks, a second activity, dinner after lunch,
    then the activity) until the modest version fits. `keep(slot)` = asked for."""
    slots = list(slots)
    if not budget or budget <= 0:
        return slots
    pp = budget / max(1, party_size or 1)

    def victim():
        for kind in ("dessert", "cafe", "drinks", "leisure"):
            if kind in slots and not keep(kind):
                return len(slots) - 1 - slots[::-1].index(kind)
        acts = [i for i, s in enumerate(slots) if s == "activity"]
        if len(acts) > 1:
            return acts[-1]
        if "lunch" in slots and "dinner" in slots and not keep("dinner"):
            return slots.index("dinner")
        if acts and not keep("activity"):
            return acts[0]
        return None

    while len(slots) > 2 and sum(MODEST.get(s, 15) for s in slots) > pp:
        i = victim()
        if i is None:
            break
        slots.pop(i)
    return slots
