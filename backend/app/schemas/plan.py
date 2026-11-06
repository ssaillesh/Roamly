from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PlanStop(BaseModel):
    slot: str
    label: str
    icon: str
    name: str
    lat: float
    lng: float
    rating: float | None = None
    price: str | None = None
    address: str | None = None
    image: str | None = None
    url: str | None = None
    source: str | None = None
    est_cost: int
    categories: list[str] = []
    why: str | None = None
    time: str | None = None
    # The stored venue ("ovt:…"): edits and feedback name the exact place by it.
    vid: str | None = None
    # Opening hours (OpenStreetMap opening_hours syntax), when known.
    hours: str | None = None
    # How lively the stop is (1–5); the narration builds around the liveliest.
    arousal: float | None = None
    # A place the user asked for by name: a shorter plan never drops it.
    requested: bool = False


class Hop(BaseModel):
    """Getting from one stop to the next (services/travel.py estimates)."""
    km: float
    minutes: int
    mode: str            # walk | transit | car


class Center(BaseModel):
    lat: float
    lng: float


class Event(BaseModel):
    name: str
    venue: str | None = None
    lat: float | None = None
    lng: float | None = None
    date: str | None = None
    time: str | None = None
    category: str | None = None
    url: str | None = None
    price_min: float | None = None
    price_max: float | None = None
    image: str | None = None
    start: str | None = None          # ISO; the page turns it into "on now" / "in 40 min"
    end: str | None = None
    free: bool | None = None          # None = unknown (not every source says)
    source: str | None = None         # "ticketmaster" | "city_toronto" | "rova" | "seatgeek"
    distance_km: float | None = None
    genre: str | None = None          # "Dance/Electronic", "Hip-Hop/Rap"…
    subgenre: str | None = None
    adults_only: bool | None = None   # 19+ (legal drinking age enforced)
    description: str | None = None    # Rova-listed events
    kind: str | None = None           # club_night | special_guest | party | holiday …
    special_day: str | None = None    # services/special_days key
    tz: str | None = None             # the place's time zone: times show in it, not the phone's


class Plan(BaseModel):
    vibe: str
    budget: float
    party_size: int
    currency: str = "USD"
    estimated_cost: int
    center: Center
    intro: str | None = None
    tip: str | None = None
    weather: str | None = None
    walk_km: float | None = None
    # When the first stop starts (ISO, local time): edits re-time the stops from it.
    start: str | None = None
    # True when the AI didn't pick the stops (it was busy or unavailable): the page labels
    # it a "quick version" of the plan.
    quick: bool = False
    stops: list[PlanStop]
    events: list[Event] = Field(default_factory=list)
    day: int | None = None
    # The brief the plan was made to (walk / transit / car / any; what they asked for),
    # kept so an edit works to it without the page sending it again.
    transport: str | None = None
    interests: str | None = None
    # One leg between each pair of stops, in order (empty on older plans).
    hops: list[Hop] = Field(default_factory=list)


class ChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    lat: float | None = None
    lng: float | None = None
    # Randomizer controls: venue names already shown this session (never repeat),
    # a client-chosen seed so each re-roll is a genuinely different draw, and an
    # explicit surprise mode that samples adventurously instead of top-ranked.
    exclude: list[str] = Field(default_factory=list)
    seed: int | None = None
    surprise: bool = False
    # Structured preferences from tappable chips (the planner's "ready card"):
    # budget (total), vibe, party_size, group_type, time_of_day, transport,
    # location, interests, avoid, dietary, days. When present, the LLM extraction
    # round trip is skipped entirely — faster and deterministic.
    prefs: dict | None = None
    # The page's current "Planning around" settings (same shape as `prefs`), sent
    # with typed messages: whatever the message doesn't say is taken from here, so
    # the bar, the chat and the planner always work from the same values.
    defaults: dict | None = None
    # The browser's IANA time zone (e.g. "America/Toronto") for "tonight" and
    # "this weekend" windows.
    tz: str | None = None
    # One id per conversation on the page, so the admin dashboard can group turns.
    session_id: str | None = Field(default=None, max_length=64)
    # The plan on screen, so "swap the bar" edits it instead of starting over.
    current_plan: dict | None = None
    # `messages` is the whole conversation; messages[turn_start:] is the current
    # request (everything since the last plan/events answer). The keyword fallback
    # reads only the current request; the LLM reads it all.
    turn_start: int = Field(default=0, ge=0)
    # Questions already asked in this conversation (ChatResponse.ask) — never re-asked.
    asked: list[str] = Field(default_factory=list, max_length=20)


class ChatResponse(BaseModel):
    # "question" → need more info; "itinerary" → plan ready; "events" → what's on;
    # "message" → info/error
    type: str
    message: str
    quick_replies: list[str] = Field(default_factory=list)
    plan: Plan | None = None          # single-day
    days: list[Plan] = Field(default_factory=list)  # multi-day trip
    events: list[Event] = Field(default_factory=list)   # "what's happening" results
    # What the planner took from this request (vibe, group_type, party_size, budget,
    # time_of_day, transport, radius_km, location) — the page's "Planning around"
    # bar mirrors it, so a typed "cheap date in Kensington" visibly updates the bar.
    understood: dict | None = None
    window: str | None = None                          # now | tonight | weekend
    title: str | None = None
    ask: str | None = None        # which question this is ("location", "budget_scope"); sent back in `asked`
    intent: str | None = None     # new_plan | edit_plan | events | ask_about_plan | chitchat


# ---- picker (build-your-own itinerary) ----
class Option(BaseModel):
    slot: str
    name: str
    lat: float | None = None
    lng: float | None = None
    rating: float | None = None
    review_count: int | None = None
    price: str | None = None
    categories: list[str] = Field(default_factory=list)
    address: str | None = None
    image: str | None = None
    url: str | None = None
    source: str | None = None
    est_cost: int
    arousal: float | None = None
    tag: str | None = None            # 🔥 / ⭐ / 💎 / 😴
    tag_label: str | None = None


class Section(BaseModel):
    key: str
    label: str
    icon: str
    hint: str
    options: list[Option]


class OptionsResponse(BaseModel):
    ok: bool = True
    message: str | None = None
    location: dict | None = None      # {lat, lng}
    context: dict = Field(default_factory=dict)   # vibe, budget, party_size, …
    sections: list[Section] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)


class EditOp(BaseModel):
    """One change to a plan (services/plan_edit.py has what each does)."""
    model_config = ConfigDict(extra="ignore")
    op: Literal["remove", "swap", "add", "move", "set_start", "set_party", "set_length", "add_event"]
    stop: int | str | None = Field(default=None)           # 1-based, or "last"
    name: str | None = Field(default=None, max_length=120)
    slot: str | None = Field(default=None, max_length=20)
    wish: str | None = Field(default=None, max_length=200)
    position: int | None = Field(default=None, ge=1, le=10)
    to: int | None = Field(default=None, ge=1, le=10)
    time: str | None = Field(default=None, max_length=20)
    party_size: int | None = Field(default=None, ge=1, le=20)
    stops: int | None = Field(default=None, ge=2, le=8)
    event: dict | None = None


class EditRequest(BaseModel):
    """Change the plan on screen directly (a button, not a typed sentence)."""
    plan: Plan
    ops: list[EditOp] = Field(min_length=1, max_length=10)
    session_id: str | None = Field(default=None, max_length=64)
    seed: int | None = None


class FeedbackStop(BaseModel):
    model_config = ConfigDict(extra="ignore")
    vid: str | None = Field(default=None, max_length=80)
    slot: str | None = Field(default=None, max_length=20)
    name: str | None = Field(default=None, max_length=200)


class FeedbackRequest(BaseModel):
    """POST /plan/feedback: the plan on screen was saved. Only the venue ids matter."""
    event: Literal["saved"]
    stops: list[FeedbackStop] = Field(min_length=1, max_length=40)


class ReviewRequest(BaseModel):
    """POST /plan/review: 1–5 stars for the plan on screen (or a trip's days), and an
    optional comment. Sending the same plan again updates the rating; leaving `comment`
    out keeps the one already sent."""
    plan: Plan | None = None
    days: list[Plan] = Field(default_factory=list, max_length=5)
    stars: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=1000)
    session_id: str | None = Field(default=None, max_length=64)


class ReviewOut(BaseModel):
    id: str
    stars: int
    comment: str | None = None


class BuildRequest(BaseModel):
    selections: list[dict]            # picked Option/event dicts (flexible shape)
    session_id: str | None = Field(default=None, max_length=64)
    lat: float
    lng: float
    party_size: int = 2
    budget: float = 0
    vibe: str = "romantic"
    interests: str = ""
    group_type: str | None = None
    time_of_day: str | None = None
    dietary: str = ""
