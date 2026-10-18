"""
How many times to chase, how often, and when to stop emailing.

The failure mode a chaser has to be designed against is not forgetting to
chase. It is chasing so reliably that the emails stop being read. A daily
automated reminder with the same subject line gets a rule in Outlook within a
fortnight, and after that the system is sending mail into a folder and
reporting it as follow-up.

So three rules, and the third is the one that matters:

1. **A cooldown.** No request is chased twice inside two working days. A bank
   that is one day late and chased on two consecutive mornings learns that the
   sender is automated.
2. **The message changes.** A second chase that reads exactly like the first
   says nobody is reading the replies either.
3. **It ends.** After the second chase the request stops being emailed and goes
   on a list for a person to raise with the relationship manager. Automation
   that escalates to a human at a defined point is credible. Automation that
   keeps sending forever is a robot having an argument with a filter.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path

LOG_PATH = Path("data/chases.json")

# Working days between chases on the same request. Shorter than any SLA here,
# because the point of the gap is to let a reply arrive, not to re-serve the
# clock.
COOLDOWN_DAYS = 2


class Stage(str, Enum):
    FIRST = "first"  # a polite nudge, states the date and the SLA
    SECOND = "second"  # names the consequence and asks for a date
    ESCALATE = "escalate"  # stop emailing; a person takes it to the RM

    @property
    def is_email(self) -> bool:
        return self is not Stage.ESCALATE


@dataclass
class Chase:
    request_id: str
    acquirer_code: str
    stage: Stage
    sent_on: str  # ISO date
    round_index: int
    days_over_at_send: int
    # Filled in when the bank eventually moves, so the effect of chasing can be
    # measured rather than assumed.
    answered_on: str = ""
    working_days_to_answer: int | None = None


@dataclass
class ChaseLog:
    chases: list[Chase] = field(default_factory=list)

    def for_request(self, request_id: str) -> list[Chase]:
        return [c for c in self.chases if c.request_id == request_id]

    def for_round(self, request_id: str, round_index: int) -> list[Chase]:
        """Chases are counted per round, not per request.

        A bank that answered, asked for a document and went quiet again is on
        its second round, and the ladder starts over. Carrying the count across
        rounds would escalate a bank to a phone call for a round it has barely
        held.
        """
        return [
            c
            for c in self.chases
            if c.request_id == request_id and c.round_index == round_index
        ]

    def last_sent(self, request_id: str) -> date | None:
        sent = [date.fromisoformat(c.sent_on) for c in self.for_request(request_id)]
        return max(sent) if sent else None

    def next_stage(self, request_id: str, round_index: int) -> Stage:
        n = len(self.for_round(request_id, round_index))
        if n == 0:
            return Stage.FIRST
        if n == 1:
            return Stage.SECOND
        return Stage.ESCALATE

    def in_cooldown(self, request_id: str, today: date, working_days_between) -> bool:
        """True when this request was chased too recently to chase again."""
        last = self.last_sent(request_id)
        if last is None:
            return False
        return working_days_between(last, today) <= COOLDOWN_DAYS

    def record(
        self,
        request_id: str,
        acquirer_code: str,
        stage: Stage,
        round_index: int,
        days_over: int,
        on: date,
    ) -> Chase:
        chase = Chase(
            request_id=request_id,
            acquirer_code=acquirer_code,
            stage=stage,
            sent_on=on.isoformat(),
            round_index=round_index,
            days_over_at_send=days_over,
        )
        self.chases.append(chase)
        return chase

    def answered(self, request_id: str, on: date, working_days: int) -> None:
        """Close the most recent open chase on this request."""
        for chase in reversed(self.chases):
            if chase.request_id == request_id and not chase.answered_on:
                chase.answered_on = on.isoformat()
                chase.working_days_to_answer = working_days
                return

    # --- persistence -----------------------------------------------------

    def to_json(self) -> str:
        return json.dumps(
            {"chases": [{**asdict(c), "stage": c.stage.value} for c in self.chases]},
            indent=2,
        )

    def save(self, path: Path = LOG_PATH) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json() + "\n")
        return path

    @classmethod
    def from_json(cls, text: str) -> "ChaseLog":
        raw = json.loads(text)
        return cls(
            chases=[
                Chase(
                    request_id=c["request_id"],
                    acquirer_code=c["acquirer_code"],
                    stage=Stage(c["stage"]),
                    sent_on=c["sent_on"],
                    round_index=c["round_index"],
                    days_over_at_send=c["days_over_at_send"],
                    answered_on=c.get("answered_on", ""),
                    working_days_to_answer=c.get("working_days_to_answer"),
                )
                for c in raw.get("chases", [])
            ]
        )

    @classmethod
    def load(cls, path: Path = LOG_PATH) -> "ChaseLog":
        return cls.from_json(path.read_text()) if path.exists() else cls()
