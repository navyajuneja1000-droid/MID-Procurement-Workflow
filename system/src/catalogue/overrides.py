"""
Overrides, and what happened to them.

This is the half of the feature that pays for the rest. Letting ops raise a
request against a restriction is only an improvement if the choice is recorded,
because the record is what turns a static list into something that corrects
itself:

- An override that comes back **rejected** proves the entry was right, and the
  reason text the bank sent can be attached to it.
- An override that comes back **approved** proves the entry is stale. Either
  the bank changed its position or the entry was wrong in the first place, and
  either way somebody should retire it.

Without the log, the system forgets every time a person disagreed with it, and
the registry can only ever get longer. With it, the list earns its place or
gets retired on evidence.

Two deliberate constraints:

**An override needs a reason.** Not a free-text box to be filled with "ok to
proceed": a reason code, so the report can group them. Six months of "approved
by RM" against one bank is a different conversation from six months of "licence
already on file".

**The override is recorded against the entry, not just the request.** The
question worth answering is "how often has this entry been overridden and what
happened", and that cannot be reconstructed from request-level notes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path

LOG_PATH = Path("data/overrides.json")


class Reason(str, Enum):
    """Why ops went ahead. Fixed codes, so the report builds itself."""

    RISK_SIGNED_OFF = "risk_signed_off"
    LICENCE_ON_FILE = "licence_on_file"
    RM_CONFIRMED = "rm_confirmed"  # the bank's own relationship manager said yes
    EXCEPTION_AGREED = "exception_agreed"  # agreed internally, usually for a large merchant
    ENTRY_LOOKS_STALE = "entry_looks_stale"  # we think the list is out of date
    COMMERCIAL_PRIORITY = "commercial_priority"  # raised knowing the risk


class Outcome(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


@dataclass
class Override:
    request_id: str
    acquirer_code: str
    mcc: str
    entry_id: str  # the registry entry overridden, or "catalogue"
    reason: Reason
    note: str
    actor: str
    at: str  # ISO date
    outcome: Outcome = Outcome.PENDING
    outcome_detail: str = ""  # the bank's own words, where we have them
    resolved_on: str = ""

    @property
    def vindicates_entry(self) -> bool:
        """The bank refused it, so the entry was right."""
        return self.outcome is Outcome.REJECTED

    @property
    def undermines_entry(self) -> bool:
        """The bank took it, so the entry is wrong or out of date."""
        return self.outcome is Outcome.APPROVED


@dataclass
class OverrideLog:
    overrides: list[Override] = field(default_factory=list)

    def record(
        self,
        request_id: str,
        acquirer_code: str,
        mcc: str,
        entry_id: str,
        reason: Reason,
        actor: str,
        note: str = "",
        on: date | None = None,
    ) -> Override:
        on = on or date.today()
        if any(o.request_id == request_id for o in self.overrides):
            raise ValueError(f"{request_id} already carries an override")
        override = Override(
            request_id=request_id,
            acquirer_code=acquirer_code,
            mcc=mcc,
            entry_id=entry_id,
            reason=reason,
            note=note,
            actor=actor,
            at=on.isoformat(),
        )
        self.overrides.append(override)
        return override

    def resolve(
        self, request_id: str, outcome: Outcome, detail: str = "",
        on: date | None = None,
    ) -> Override:
        """Attach what the bank actually did.

        Called from the reply parser rather than typed in, so the outcome is
        whatever the bank said and not whoever remembered to update it.
        """
        on = on or date.today()
        for o in self.overrides:
            if o.request_id == request_id:
                o.outcome = outcome
                o.outcome_detail = detail
                o.resolved_on = on.isoformat()
                return o
        raise KeyError(f"no override recorded for {request_id}")

    def for_entry(self, entry_id: str) -> list[Override]:
        return [o for o in self.overrides if o.entry_id == entry_id]

    def history_for(self, entry_id: str) -> str:
        """One line for the person deciding right now.

        This is what makes the flag worth reading rather than clicking past:
        not "restricted category" but "overridden four times, all four
        approved", which says plainly that the entry is probably wrong.
        """
        hits = self.for_entry(entry_id)
        if not hits:
            return "never overridden"
        approved = sum(1 for o in hits if o.undermines_entry)
        rejected = sum(1 for o in hits if o.vindicates_entry)
        pending = sum(1 for o in hits if o.outcome is Outcome.PENDING)
        bits = [f"overridden {len(hits)} time{'' if len(hits) == 1 else 's'}"]
        if approved:
            bits.append(f"{approved} approved")
        if rejected:
            bits.append(f"{rejected} rejected")
        if pending:
            bits.append(f"{pending} still open")
        return ", ".join(bits)

    # --- the report ------------------------------------------------------

    def verdict_on(self, entry_id: str) -> str:
        """What the evidence says about one entry.

        Deliberately cautious. One approval is not proof a bank changed its
        policy: a single application can slip through underwriting for reasons
        nobody records. Three with none refused is a pattern worth acting on.
        """
        hits = [o for o in self.for_entry(entry_id) if o.outcome is not Outcome.PENDING]
        if not hits:
            return "no evidence yet"
        approved = sum(1 for o in hits if o.undermines_entry)
        rejected = sum(1 for o in hits if o.vindicates_entry)
        if rejected and not approved:
            return "entry holds up"
        if approved >= 3 and not rejected:
            return "looks stale, consider retiring"
        if approved and not rejected:
            return "possibly stale, watch it"
        return "mixed, keep it"

    def by_reason(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.overrides:
            out[o.reason.value] = out.get(o.reason.value, 0) + 1
        return out

    def outcome_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.overrides:
            out[o.outcome.value] = out.get(o.outcome.value, 0) + 1
        return out

    @property
    def rejection_rate(self) -> float | None:
        """Of the overrides that got an answer, how many the bank refused.

        The single most useful number here. A low rate says the restrictions
        are over-cautious and ops is right to push. A high rate says ops is
        burning response rounds on applications that were never going to land,
        and the warning should probably be a block.
        """
        answered = [
            o for o in self.overrides
            if o.outcome in (Outcome.APPROVED, Outcome.REJECTED)
        ]
        if not answered:
            return None
        return sum(1 for o in answered if o.vindicates_entry) / len(answered)

    # --- persistence -----------------------------------------------------

    def to_json(self) -> str:
        return json.dumps(
            {
                "overrides": [
                    {**asdict(o), "reason": o.reason.value, "outcome": o.outcome.value}
                    for o in self.overrides
                ]
            },
            indent=2,
        )

    def save(self, path: Path = LOG_PATH) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json() + "\n")
        return path

    @classmethod
    def from_json(cls, text: str) -> "OverrideLog":
        raw = json.loads(text)
        return cls(
            overrides=[
                Override(
                    request_id=o["request_id"],
                    acquirer_code=o["acquirer_code"],
                    mcc=o["mcc"],
                    entry_id=o["entry_id"],
                    reason=Reason(o["reason"]),
                    note=o.get("note", ""),
                    actor=o["actor"],
                    at=o["at"],
                    outcome=Outcome(o.get("outcome", "pending")),
                    outcome_detail=o.get("outcome_detail", ""),
                    resolved_on=o.get("resolved_on", ""),
                )
                for o in raw.get("overrides", [])
            ]
        )

    @classmethod
    def load(cls, path: Path = LOG_PATH) -> "OverrideLog":
        if path.exists():
            return cls.from_json(path.read_text())
        return cls()


# Only a rejection on the category itself says anything about the entry. A
# rejection for a broken payment flow is a rejection of the merchant, and
# counting it as proof the category is barred would have the registry grow on
# evidence that has nothing to do with it.
CATEGORY_REJECTIONS = {"restricted_category", "restricted_mcc", "incorrect_mcc"}


def outcome_from_rejection(reason: str | None) -> tuple[Outcome, str]:
    """Translate what the bank said into evidence about the entry.

    Three outcomes, not two. Approved and category-refused are both evidence.
    Refused for an unrelated reason is neither, so it is recorded as withdrawn
    rather than counted as a point for either side.
    """
    if reason is None:
        return Outcome.APPROVED, "bank approved despite the restriction on file"
    if reason in CATEGORY_REJECTIONS:
        return Outcome.REJECTED, f"bank refused on the category ({reason})"
    return (
        Outcome.WITHDRAWN,
        f"refused for an unrelated reason ({reason}), says nothing about the entry",
    )


def override_and_raise(
    request,
    assessment,
    reason: Reason,
    actor: str,
    log: OverrideLog,
    at,
    note: str = "",
) -> Override:
    """Record the decision in both places at once.

    The event and the log entry are written together because they answer
    different questions and drifting apart makes both useless: the event log is
    the audit trail of what this request did, and the override log is the
    evidence base for whether the entry deserves to exist.
    """
    from ..model import Event, EventType

    entry = assessment.deciding_entry
    entry_id = entry.entry_id if entry else "catalogue"

    override = log.record(
        request_id=request.request_id,
        acquirer_code=request.acquirer.code,
        mcc=request.mcc,
        entry_id=entry_id,
        reason=reason,
        actor=actor,
        note=note or assessment.explain(),
        on=at.date() if hasattr(at, "date") else at,
    )
    request.add(
        Event(
            event_type=EventType.RESTRICTION_OVERRIDDEN,
            at=at,
            note=f"{reason.value} by {actor}; entry {entry_id}",
        )
    )
    return override
