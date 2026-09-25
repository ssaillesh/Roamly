from pydantic import BaseModel, Field


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
    stops: list[PlanStop]
    events: list[Event] = Field(default_factory=list)
    day: int | None = None


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


class ChatResponse(BaseModel):
    # "question" → need more info; "itinerary" → plan ready; "message" → info/error
    type: str
    message: str
    quick_replies: list[str] = Field(default_factory=list)
    plan: Plan | None = None          # single-day
    days: list[Plan] = Field(default_factory=list)  # multi-day trip
    title: str | None = None


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


class BuildRequest(BaseModel):
    selections: list[dict]            # picked Option/event dicts (flexible shape)
    lat: float
    lng: float
    party_size: int = 2
    budget: float = 0
    vibe: str = "romantic"
    interests: str = ""
    group_type: str | None = None
    time_of_day: str | None = None
    dietary: str = ""
