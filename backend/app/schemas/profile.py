from pydantic import BaseModel, Field


class SurveyQuestion(BaseModel):
    id: str
    section: str
    type: str                       # "single" | "multi"
    q: str
    hint: str | None = None
    optional: bool = False
    options: list[dict]             # [{id, label}]


class SurveyOut(BaseModel):
    version: int
    questions: list[SurveyQuestion]


class TasteProfileOut(BaseModel):
    answers: dict = Field(default_factory=dict)
    completed: bool = False
    missing: list[str] = Field(default_factory=list)   # required question ids not yet answered
    archetype: dict | None = None                       # {emoji, title} once complete
    summary: list[str] = Field(default_factory=list)
    # Planner defaults the profile implies — the planner page pre-fills its
    # "ready card" (crew, vibe, budget, time, transport) from these.
    defaults: dict = Field(default_factory=dict)


class TasteProfileUpdate(BaseModel):
    answers: dict
    # False (default): merge into saved answers — the survey autosaves one answer
    # at a time. True: overwrite everything (e.g. a full re-take).
    replace: bool = False
