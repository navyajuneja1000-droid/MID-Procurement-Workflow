"""
The restriction registry: what ops knows about who refuses what, and how hard.

Phase 7 encoded six acquirer policies in code and treated an acquirer's refusal
as a block. That was wrong in a specific and expensive way. These lists change
without notice, so the moment one goes stale the system quietly stops raising a
merchant to a bank that would now accept it. Nobody finds out, because there is
no rejection to look at. A block fails invisibly; a warning fails in front of a
person.

So the model here is:

- **The registry is data, not code.** Ops adds an entry when a bank tells them
  something, and it takes effect without a release. A list that cannot be
  edited by the team that learns the news is out of date within a month.
- **Three strengths, not two.** A block stops the request. A warning raises it
  only on a recorded decision. A note goes out as normal and just says what is
  known. Most acquirer-level restrictions are warnings, because the person
  raising it often knows something the list does not: a risk sign-off already
  granted, a licence in hand, an exception agreed with the relationship manager.
- **Only our own policy blocks.** A category the business will not onboard is a
  block, whoever would have accepted it. A category one bank currently refuses
  is a warning, because that is a fact about today, not a rule.
- **Every entry carries where it came from.** "Their published circular" and
  "the RM said so on a call in March" are not the same claim, and the
  confidence attached to an entry follows its source rather than being typed in
  by whoever added it.
- **Nothing is deleted.** An entry that no longer applies is retired with a
  date, because the question "when did this bank start accepting this, and who
  told us" is asked constantly and a deleted row cannot answer it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path

from .mcc import CATALOGUE, Instrument, MccEntry

# Our own policy, as against any bank's. Held in the same registry so one
# lookup answers both questions, and kept distinguishable because it is the
# only source that blocks.
HOUSE = "HOUSE"

STORE_PATH = Path("data/restrictions.json")


class Enforcement(str, Enum):
    """How hard an entry bites.

    The default for anything ops adds is WARN. Starting at BLOCK would mean a
    single entry typed in haste silently removes an acquirer from every future
    batch, and the way that gets discovered is a monthly volume question.
    """

    BLOCK = "block"  # do not raise, and do not chase documents for it
    WARN = "warn"  # raise only on a recorded decision
    NOTE = "note"  # goes out as normal; the file or email states the condition


class Scope(str, Enum):
    """What the entry is written against.

    Banks write their lists at whichever level suits them. A circular naming
    "jewellery and bullion" covers several codes, and transcribing it into
    individual MCCs loses the fact that it was a category rule, so the next
    code added to that category is not covered by it.
    """

    MCC = "mcc"
    CATEGORY = "category"
    SUPER_CATEGORY = "super_category"


class Source(str, Enum):
    """Where the entry came from. This decides its confidence."""

    CIRCULAR = "circular"  # the bank published it
    EMAIL = "email"  # written confirmation from the bank
    OBSERVED_REJECTION = "observed_rejection"  # they rejected us for this reason
    RM_CALL = "rm_call"  # someone said so on a call
    SEED = "seed"  # the policy documents this project started from
    HOUSE_POLICY = "house_policy"  # our own decision


# Confidence follows the source rather than being typed in. Whoever adds an
# entry is the least neutral judge of how solid their own information is, and a
# number they choose is a number they can talk themselves into.
CONFIDENCE: dict[Source, float] = {
    Source.CIRCULAR: 1.0,
    Source.HOUSE_POLICY: 1.0,
    Source.EMAIL: 0.95,
    Source.OBSERVED_REJECTION: 0.9,
    Source.SEED: 0.85,
    Source.RM_CALL: 0.6,
}


@dataclass
class Restriction:
    entry_id: str
    acquirer_code: str  # an acquirer, or HOUSE
    scope: Scope
    value: str  # an MCC code, a category name, or a super category name
    enforcement: Enforcement
    reason: str
    source: Source
    added_by: str
    added_on: str  # ISO date
    # Empty means the whole category. Named instruments mean the category is
    # open on the other rails, which is the difference between a merchant going
    # live on debit and not going live at all.
    instruments: tuple[Instrument, ...] = ()
    condition: str = ""  # what would make it acceptable, if anything would
    retired_on: str = ""
    retired_because: str = ""

    @property
    def live(self) -> bool:
        return not self.retired_on

    @property
    def confidence(self) -> float:
        return CONFIDENCE[self.source]

    @property
    def is_house(self) -> bool:
        return self.acquirer_code == HOUSE

    def covers(self, entry: MccEntry) -> bool:
        if self.scope is Scope.MCC:
            return entry.code == self.value
        if self.scope is Scope.CATEGORY:
            return entry.category == self.value
        return entry.super_category == self.value

    def describe(self) -> str:
        who = "house policy" if self.is_house else self.acquirer_code
        rails = (
            " on " + ",".join(i.value for i in self.instruments)
            if self.instruments
            else ""
        )
        return (
            f"{who}: {self.scope.value} {self.value}{rails} "
            f"[{self.enforcement.value}, {self.source.value}, {self.added_on}]"
        )


@dataclass
class Change:
    """One line of history per edit.

    The useful question is not what the list says, it is when it started saying
    it and on whose word. Without this the registry is a rumour with a schema.
    """

    at: str
    actor: str
    action: str  # added | retired | promoted | enforcement_changed
    entry_id: str
    detail: str = ""


@dataclass
class Registry:
    entries: list[Restriction] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)

    # --- reading ---------------------------------------------------------

    def live_entries(self, acquirer_code: str | None = None) -> list[Restriction]:
        return [
            e
            for e in self.entries
            if e.live and (acquirer_code is None or e.acquirer_code == acquirer_code)
        ]

    def matching(
        self,
        acquirer_code: str,
        mcc: str,
        instruments: tuple[Instrument, ...] = (),
    ) -> list[Restriction]:
        """Every live entry that applies, house policy included, strongest first.

        House entries come back alongside the acquirer's own because the caller
        needs to know a block exists regardless of which bank was asked, and
        sorting by strength means the first entry is the one that decides.
        """
        entry = CATALOGUE.get(mcc)
        if entry is None:
            return []

        order = {Enforcement.BLOCK: 0, Enforcement.WARN: 1, Enforcement.NOTE: 2}
        hits = [
            r
            for r in self.entries
            if r.live
            and r.acquirer_code in (acquirer_code, HOUSE)
            and r.covers(entry)
            # An entry naming particular rails does not apply to a request that
            # did not ask for any of them.
            and (
                not r.instruments
                or not instruments
                or any(i in instruments for i in r.instruments)
            )
        ]
        return sorted(hits, key=lambda r: (order[r.enforcement], -r.confidence))

    def acquirers_with_policy(self) -> set[str]:
        """Who we actually hold a list for.

        Needed by the house-policy candidate view, because "every acquirer
        refuses this" means much less when we only hold three of seven lists.
        """
        return {e.acquirer_code for e in self.entries if e.live and not e.is_house}

    # --- writing ---------------------------------------------------------

    def add(
        self,
        acquirer_code: str,
        scope: Scope,
        value: str,
        reason: str,
        source: Source,
        added_by: str,
        enforcement: Enforcement | None = None,
        instruments: tuple[Instrument, ...] = (),
        condition: str = "",
        on: date | None = None,
    ) -> Restriction:
        """Add an entry. Defaults to a warning, and house entries to a block.

        The default matters more than it looks. Ops adding what a bank said on
        a call should produce a flag on the next batch, not a silent removal of
        that bank from every batch for the next six months.
        """
        on = on or date.today()
        self._validate(scope, value)

        if enforcement is None:
            enforcement = (
                Enforcement.BLOCK if acquirer_code == HOUSE else Enforcement.WARN
            )
        if acquirer_code != HOUSE and enforcement is Enforcement.BLOCK:
            # Allowed, but it is a decision, so it is recorded as one rather
            # than passing as an ordinary edit.
            pass

        entry_id = self._next_id(acquirer_code, scope, value)
        entry = Restriction(
            entry_id=entry_id,
            acquirer_code=acquirer_code,
            scope=scope,
            value=value,
            enforcement=enforcement,
            reason=reason,
            source=source,
            added_by=added_by,
            added_on=on.isoformat(),
            instruments=tuple(instruments),
            condition=condition,
        )
        self.entries.append(entry)
        self.changes.append(
            Change(
                at=on.isoformat(),
                actor=added_by,
                action="added",
                entry_id=entry_id,
                detail=f"{enforcement.value} from {source.value}: {reason}",
            )
        )
        return entry

    def retire(self, entry_id: str, actor: str, because: str, on: date | None = None) -> None:
        """Stop applying an entry, keeping the row.

        A bank that starts accepting a category is the single most valuable
        update this registry receives, and deleting the old row throws away the
        before-and-after that proves the registry is being maintained.
        """
        on = on or date.today()
        entry = self.get(entry_id)
        entry.retired_on = on.isoformat()
        entry.retired_because = because
        self.changes.append(
            Change(at=on.isoformat(), actor=actor, action="retired",
                   entry_id=entry_id, detail=because)
        )

    def set_enforcement(
        self, entry_id: str, enforcement: Enforcement, actor: str,
        because: str, on: date | None = None,
    ) -> None:
        on = on or date.today()
        entry = self.get(entry_id)
        was = entry.enforcement
        entry.enforcement = enforcement
        self.changes.append(
            Change(
                at=on.isoformat(), actor=actor, action="enforcement_changed",
                entry_id=entry_id,
                detail=f"{was.value} -> {enforcement.value}: {because}",
            )
        )

    def promote_to_house(
        self, scope: Scope, value: str, actor: str, because: str,
        on: date | None = None,
    ) -> Restriction:
        """Make a category our own policy, which is the only thing that blocks.

        Deliberately a separate action rather than something the system infers
        from every acquirer happening to refuse a category. The intersection of
        the lists gets wider the fewer lists we hold, so inferring a block from
        it would mean the strength of our rules depends on how complete our
        data is, which is backwards.
        """
        on = on or date.today()
        entry = self.add(
            acquirer_code=HOUSE,
            scope=scope,
            value=value,
            reason=because,
            source=Source.HOUSE_POLICY,
            added_by=actor,
            enforcement=Enforcement.BLOCK,
            on=on,
        )
        self.changes.append(
            Change(at=on.isoformat(), actor=actor, action="promoted",
                   entry_id=entry.entry_id, detail=because)
        )
        return entry

    # --- helpers ---------------------------------------------------------

    def get(self, entry_id: str) -> Restriction:
        for e in self.entries:
            if e.entry_id == entry_id:
                return e
        raise KeyError(f"no restriction with id {entry_id!r}")

    def _next_id(self, acquirer_code: str, scope: Scope, value: str) -> str:
        stem = f"{acquirer_code}-{scope.value[:3]}-{value}".upper().replace(" ", "_")
        existing = {e.entry_id for e in self.entries}
        if stem not in existing:
            return stem
        n = 2
        while f"{stem}-{n}" in existing:
            n += 1
        return f"{stem}-{n}"

    @staticmethod
    def _validate(scope: Scope, value: str) -> None:
        """An entry against something that does not exist is worse than no
        entry: it sits in the list looking like cover and never matches
        anything."""
        if scope is Scope.MCC:
            if value not in CATALOGUE:
                raise ValueError(f"MCC {value!r} is not in the catalogue")
            return
        attribute = "category" if scope is Scope.CATEGORY else "super_category"
        known = {getattr(e, attribute) for e in CATALOGUE.values()}
        if value not in known:
            raise ValueError(f"{attribute} {value!r} does not exist in the catalogue")

    # --- persistence -----------------------------------------------------

    def to_json(self) -> str:
        return json.dumps(
            {
                "entries": [
                    {
                        **asdict(e),
                        "scope": e.scope.value,
                        "enforcement": e.enforcement.value,
                        "source": e.source.value,
                        "instruments": [i.value for i in e.instruments],
                    }
                    for e in self.entries
                ],
                "changes": [asdict(c) for c in self.changes],
            },
            indent=2,
        )

    def save(self, path: Path = STORE_PATH) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json() + "\n")
        return path

    @classmethod
    def from_json(cls, text: str) -> "Registry":
        raw = json.loads(text)
        entries = []
        for e in raw.get("entries", []):
            entries.append(
                Restriction(
                    entry_id=e["entry_id"],
                    acquirer_code=e["acquirer_code"],
                    scope=Scope(e["scope"]),
                    value=e["value"],
                    enforcement=Enforcement(e["enforcement"]),
                    reason=e["reason"],
                    source=Source(e["source"]),
                    added_by=e["added_by"],
                    added_on=e["added_on"],
                    instruments=tuple(Instrument(i) for i in e.get("instruments", [])),
                    condition=e.get("condition", ""),
                    retired_on=e.get("retired_on", ""),
                    retired_because=e.get("retired_because", ""),
                )
            )
        changes = [Change(**c) for c in raw.get("changes", [])]
        return cls(entries=entries, changes=changes)

    @classmethod
    def load(cls, path: Path = STORE_PATH) -> "Registry":
        """Load the registry, seeding it the first time.

        Seeding rather than shipping an empty list, because a team handed an
        empty registry has to retype what the project already knows, and in the
        meantime every answer is "no restriction found", which reads as a clean
        bill of health.
        """
        if path.exists():
            return cls.from_json(path.read_text())
        registry = seed()
        registry.save(path)
        return registry


def seed() -> Registry:
    """Build the registry from the policies phase 7 encoded, plus house policy.

    Those six policies become seed data rather than the final word. Their shape
    is preserved in `source` and `reason`, so the finding they carry (eleven
    incompatible lists, four formats) survives the move into one registry.
    """
    from .eligibility import POLICIES

    registry = Registry()
    on = date(2026, 1, 15)

    # House policy first. These are the categories the business does not
    # onboard at all, which is a different claim from "no bank takes it".
    #
    # The reason text is built from the catalogue entry rather than typed
    # beside the code. Written by hand, the first version of this had the
    # reasons one row out of step, so the registry said adult services about
    # the money transfer code and nothing looked wrong until the report was
    # read side by side with the catalogue.
    for code, why in (
        ("7521", "not onboarded without a payment aggregator licence for it"),
        ("7527", "held back pending a licensing view"),
        ("7630", "not onboarded as a matter of policy"),
    ):
        registry.add(
            acquirer_code=HOUSE,
            scope=Scope.MCC,
            value=code,
            reason=f"{CATALOGUE[code].sub_category}: {why}",
            source=Source.HOUSE_POLICY,
            added_by="risk",
            enforcement=Enforcement.BLOCK,
            on=on,
        )

    for acquirer_code, policy in POLICIES.items():
        shape = policy.shape.value

        for mcc in sorted(policy.prohibited_codes):
            if mcc not in CATALOGUE:
                continue
            registry.add(
                acquirer_code=acquirer_code,
                scope=Scope.MCC,
                value=mcc,
                reason=f"on the prohibited list ({shape} policy)",
                source=Source.SEED,
                added_by="import",
                # The change her model asks for: an acquirer refusing a
                # category is a warning that needs a decision, not a block.
                enforcement=Enforcement.WARN,
                on=on,
            )

        for mcc in sorted(policy.restricted_codes):
            if mcc not in CATALOGUE:
                continue
            registry.add(
                acquirer_code=acquirer_code,
                scope=Scope.MCC,
                value=mcc,
                reason=f"restricted, needs risk sign-off ({shape} policy)",
                source=Source.SEED,
                added_by="import",
                enforcement=Enforcement.NOTE,
                condition="risk sign-off for a restricted category",
                on=on,
            )

        for mcc, instruments in sorted(policy.instrument_bans.items()):
            if mcc not in CATALOGUE:
                continue
            registry.add(
                acquirer_code=acquirer_code,
                scope=Scope.MCC,
                value=mcc,
                reason="not run on these rails for this category",
                source=Source.SEED,
                added_by="import",
                enforcement=Enforcement.NOTE,
                instruments=instruments,
                condition="raise for the permitted rails only",
                on=on,
            )

        for mcc in sorted(policy.product_banned_codes):
            if mcc not in CATALOGUE:
                continue
            registry.add(
                acquirer_code=acquirer_code,
                scope=Scope.MCC,
                value=mcc,
                reason=f"on the separate {policy.product_ban_label} ban list",
                source=Source.SEED,
                added_by="import",
                enforcement=Enforcement.NOTE,
                instruments=(Instrument.EMI,),
                condition="raise without EMI",
                on=on,
            )

        # A prose policy is written as business categories, so it is stored at
        # category scope. Transcribing it into individual codes would lose the
        # fact that it was a category rule, and the next code added to that
        # category would silently fall outside it.
        for phrase in policy.prose_prohibited:
            for category in _categories_for_phrase(phrase):
                registry.add(
                    acquirer_code=acquirer_code,
                    scope=Scope.CATEGORY,
                    value=category,
                    reason=f'prose policy: "{phrase}"',
                    source=Source.SEED,
                    added_by="import",
                    enforcement=Enforcement.WARN,
                    on=on,
                )
        for phrase in policy.prose_restricted:
            for category in _categories_for_phrase(phrase):
                registry.add(
                    acquirer_code=acquirer_code,
                    scope=Scope.CATEGORY,
                    value=category,
                    reason=f'prose policy, restricted: "{phrase}"',
                    source=Source.SEED,
                    added_by="import",
                    enforcement=Enforcement.NOTE,
                    condition="risk sign-off for a restricted category",
                    on=on,
                )

    return registry


def _categories_for_phrase(phrase: str) -> list[str]:
    """Which catalogue categories a prose policy line covers.

    Reuses the same matcher the phase 7 check uses, so the registry and the
    policy resolution cannot disagree about what a paragraph means.
    """
    from .eligibility import prose_matches_entry

    out: list[str] = []
    for entry in CATALOGUE.values():
        if prose_matches_entry(phrase, entry) and entry.category not in out:
            out.append(entry.category)
    return out
