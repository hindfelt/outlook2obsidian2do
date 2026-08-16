"""Pydantic models. The *Output models are the JSON schemas the Claude API is
constrained to via structured outputs (client.messages.parse)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Owner = Literal["me", "sender", "third_party", "unclear"]
Priority = Literal["high", "normal", "low"]


# --- Inbound from the Outlook add-in -----------------------------------------


class EmailPayload(BaseModel):
    subject: str = ""
    sender: str = ""
    body: str = ""
    received_at: str | None = None
    item_id: str | None = None
    web_link: str | None = None
    dry_run: bool = False
    # Run again even if this item_id was processed recently (see jobs.submit).
    force: bool = False


# --- Agent 1: Extractor -------------------------------------------------------


class ExtractedTask(BaseModel):
    text: str = Field(
        description="The action, imperative, one sentence, self-contained. "
        "No leading checkbox or bullet."
    )
    owner: Owner = Field(
        description="Who has to do it. 'me' = the mailbox owner, "
        "'sender' = the person who sent the email."
    )
    owner_name: str = Field(
        default="",
        description="Name of the owner if the email names one, else empty string.",
    )
    due: str = Field(
        default="",
        description="Absolute due date as YYYY-MM-DD if the email states or "
        "clearly implies one, else empty string. Never invent a date.",
    )
    priority: Priority = "normal"
    explicit: bool = Field(
        description="True if the email literally asks for this; "
        "False if it is implied."
    )
    evidence: str = Field(
        description="Short verbatim quote from the email that supports this task."
    )


class ExtractionOutput(BaseModel):
    tasks: list[ExtractedTask]
    summary: str = Field(
        default="", description="One line on what the email is about."
    )


# --- Agent 2: Router ----------------------------------------------------------


class RoutingDecision(BaseModel):
    task_index: int = Field(description="0-based index into the task list.")
    route_id: str = Field(description="Exactly one id from the route catalog.")
    # Not `Field(ge=0, le=1)`: Ollama compiles the schema to a decoding grammar,
    # and grammars cannot express numeric bounds. A local model that returns
    # -0.1 would fail validation and discard the whole (minutes-long) run over a
    # display-only field, so the value is clamped instead of rejected.
    confidence: float = Field(
        description="How sure you are, from 0.0 to 1.0 inclusive."
    )
    reason: str = Field(description="One short sentence.")

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, value: object) -> float:
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0
        if number != number:  # NaN
            return 0.0
        return min(1.0, max(0.0, number))


class RoutingOutput(BaseModel):
    decisions: list[RoutingDecision]


# --- Agent 3: Writer ----------------------------------------------------------


class FormattedTask(BaseModel):
    task_index: int
    markdown: str = Field(
        description="A single Obsidian checklist line starting with '- [ ] '."
    )


class WriterOutput(BaseModel):
    lines: list[FormattedTask]


# --- Response back to the add-in ---------------------------------------------


class WrittenTask(BaseModel):
    text: str
    route_id: str
    file: str
    section: str
    markdown: str
    confidence: float
    reason: str


class ProcessResponse(BaseModel):
    ok: bool
    dry_run: bool
    summary: str
    tasks: list[WrittenTask]
    files_written: list[str]
    warnings: list[str] = []
