# MID Procurement Automation

A portfolio product for fintech PM roles. It automates cards MID procurement
for a payment aggregator and reports turnaround time honestly, which the
spreadsheet it replaces cannot.

**Phases 0, 1, 3, 4, 5, 6, 7, 7a and 8 are complete**: data model, working-day
calendar, TAT engine, six invented acquirers, a synthetic dataset generator,
the dashboard, the bank reply parser, the intake readiness check, the MCC
catalogue, an editable restriction registry with recorded overrides, bank file
generation with pre-flight checks, the SLA chaser, and 283 tests.

## Run it

```bash
python -m pytest tests/ -q     # 283 tests, all rules encoded as checks
python example.py              # one merchant, two acquirers, figures to check by hand
python build_dataset.py        # writes the full synthetic dataset to data/
python parse_inbox.py          # parses data/emails and scores itself
python export_dashboard.py     # writes dashboard_data.json for the page
python run_intake.py           # per-acquirer readiness and category eligibility
python make_batch.py           # builds today's bank files and draft emails
python review_restrictions.py  # the restriction registry and override outcomes
python manage_restrictions.py --help   # add, retire and inspect restrictions
python run_chaser.py           # today's chase list, drafts, and whether chasing works
```

## The domain, in short

A payment aggregator cannot put a merchant live until an acquiring bank issues
a MID. Every merchant needs its own dedicated MID from each acquirer, and is
raised to 3 to 4 of 6 to 7 acquirers in parallel. Roughly 1,000 requests a
month, handled by 3 people, tracked in a Google Sheet updated by hand.

30 to 40% of requests bounce back for missing data. TAT per party is worked out
manually every week for a leadership review.

## Rules that are easy to get wrong

Read these before changing `src/tat.py`. They came from an operator who ran
this process, and they are not guesses.

1. **The unit of work is merchant + MCC + acquirer.** Not merchant + acquirer.
   One merchant can run several use cases, each needing its own MCC and its own
   MID request. Bank replies are matched on legal name plus MCC for this
   reason. The spreadsheet's one-row-per-merchant shape cannot hold this.

2. **Clocks overlap, in two specific places.** Ops runs alongside the acquirer,
   because getting the MID is ops' job. Onboarding runs alongside BD, because
   onboarding owns the ticket until it comes back clean. Consequence: per-party
   days sum to more than elapsed time, so **a share-of-delay pie chart would be
   wrong**. The honest visual is a timeline bar per request.

3. **Ops is measured per episode, not merged.** A request ops picks up three
   times is reported as three entries. The episode count is itself the rework
   measure. Raising to the bank splits an ops episode into solo and overlap
   phases but is *not* a new pickup, because ops never put it down.

4. **Ops time splits into solo and overlap.** Solo is the efficiency figure.
   Overlap is time ops is accountable for but does not control. Report both
   plus a consolidated figure, which is the agreed convention.

5. **2pm cut-off, on the acquirer clock only.** A batch sent after 2pm starts
   the bank's clock the next working day. Internal clocks start immediately.

6. **Working days, with a per-acquirer holiday override.** Most acquirers share
   one calendar; one or two differ.

7. **Same-day handoff counts as one day, not zero.** A party that handled and
   passed on a request still consumed a day.

8. **Deprioritisation stops every clock** and the request is excluded from TAT
   averages. But count deprioritised requests separately and show how long they
   sat first: that is ops effort that produced nothing.

9. **Approval and MID delivery are different events on different email
   threads.** The gap between them is invisible in the current tracker and sits
   inside procurement TAT. Measured here as a separate acquirer phase.

9a. **Acquirer SLA is judged per response round, not on total held time.** A
    bank that answers in 4 days, asks for a document, then answers again in 4
    days has met a 5-day SLA twice. Charging it the 8-day sum would bill the
    bank for our own turnaround between its rounds. Use
    `acquirer_worst_status_round` for breach, `acquirer_status_days` for total
    held time, and `acquirer_status_rounds` as a measure of our own data
    quality: more than one round means the application could not be decided in
    a single pass.

10. **BAU and bulk migration are reported separately.** A migration backlog
    otherwise destroys BAU averages.

11. **The merchant is not a party.** Time waiting on a merchant is charged to
    BD, as it is today. Open question: whether to add a merchant clock so BD is
    not blamed for merchant silence.

12. **Nothing from the real workplace goes in.** Invented acquirers, invented
    merchants. No real bank names, merchant names, rate cards or templates.

## Layout

```
src/model.py            Entities, enums, bounce routing. Status is derived
                        from events, never stored.
src/calendar_rules.py   Working days, holidays, the 2pm cut-off.
src/tat.py              The engine. Replays events into episodes per party.
src/identifiers.py      PAN and GSTIN generation and validation, including
                        the real GSTIN checksum and the PAN cross-check.
src/names.py            Invented name pools, safe and restricted MCCs.
src/generator.py        Synthetic merchants, requests and event histories.
src/emails.py           Bank replies in each acquirer's own messy format.
config/acquirers.py     Six invented acquirers, SLAs, field requirements,
                        holiday calendars.
tests/test_tat.py       Every TAT rule as an executable check.
tests/test_generator.py Checks that the data is honest, not just well shaped.
example.py              Worked example with figures to verify by hand.
build_dataset.py        Writes the dataset to data/.
```

## The dataset (phase 1)

`python build_dataset.py` writes to `data/`: merchants, requests, the event
log, computed TAT per request, and bank reply emails as text files with CSV
attachments. Seeded, so a demo shows the same numbers twice.

Roughly 360 merchants, 1,380 requests, 4,600 events, 650 emails. About 30% of
requests bounce, matching the real pipeline. 380 requests sit unraised in the
ops queue, which is the migration backlog.

Two properties the tests enforce, because without them every later feature
would be measured against data that flatters it:

- **Gaps are real and live on the merchant, not the request.** A missing
  signatory PAN is a fact about the merchant that blocks every acquirer asking
  for that field and no others. That is what makes partial raising possible,
  and what a readiness check has to find. Gaps split into unresolved (never
  supplied, the request is still open) and resolved (BD chased it down, so the
  record is complete even though the request bounced on the way).
- **Presence is not validity.** A small share of records carry a mistyped PAN
  or a GST issued against a different entity. Only a format check and a
  PAN-to-GST cross-check find those.

The emails are deliberately hard: each acquirer uses its own column names and
layout, status wording is free text ("Approved", "APPROVED", "Cleared", "Not
Approved"), rejection reasons are sentences rather than codes, legal names
drift from the record, and replies cover only part of a batch.

## Design decisions worth keeping

**Events, not durations.** Nothing stores "TAT = 5 days". Every figure is
derived from an immutable event list on demand. Three payoffs: the number is
never stale, a rule change recalculates history instead of losing it, and any
number can be opened up when management asks why a request took 22 days.

**Acquirer, not bank.** One bank running two processing platforms is two
acquirers here, because they are two applications with separate configuration
and separate clocks.

**Fixed reason codes.** Bounces carry a `BounceReason` that routes
automatically (documents to BD, analysis and data to onboarding). The
back-ticket-reasons report then builds itself instead of being bucketed by hand
from free-text comments.

**Config over code.** Acquirer field requirements, SLAs and holidays are config.
Adding an acquirer or handling a mandated field change is an edit, not a
rebuild.

## What comes next

| Phase | What | Depends on |
|---|---|---|
| ~~1~~ | ~~Synthetic data generator~~ **done** | |
| ~~3~~ | ~~Dashboard~~ **done**. Headline: blended ops TAT. Then per-acquirer, status counts, segment and tier splits, BAU/migration toggle, ops exception list, drill-down timeline | 0, 1 |
| ~~4~~ | ~~Bank reply parser~~ **done**. Two streams: status replies and MID delivery. Fuzzy match on legal name + MCC with confidence scoring. Apply confident rows immediately, queue the rest | 1 |
| ~~5~~ | ~~Intake~~ **done**: per-acquirer readiness check, PAN/GST format and consistency validation, one consolidated bounce | 0 |
| ~~6~~ | ~~Bank file generation, pre-flight validation, draft email~~ **done** | |
| ~~7~~ | ~~MCC catalogue and per-acquirer eligibility~~ **done** | |
| ~~7a~~ | ~~Editable restriction registry, enforcement levels, recorded overrides~~ **done** | 7 |
| ~~8~~ | ~~SLA chaser~~ **done**. Post-live website monitoring cut: see below | 3, 7 |
| 9 | PRD and case study | all |

Phase 3 is the first demoable product. Everything after it is enhancement
rather than construction.

### Notes for later phases

- **Partial raising is allowed.** A merchant ready for two acquirers but not
  the other two is raised to those two immediately. The readiness check is per
  acquirer, never all-or-nothing.
- **One batch per acquirer per day**, first thing in the morning, with an
  exception path for urgent single-merchant files.
- **The system drafts the email, a human sends it.** One deliberate checkpoint
  at the external boundary.
- **Parser confidence**: update the rows the parser is sure about immediately
  so those clocks stop, and queue the uncertain ones for review. Do not hold 35
  correct rows for 5 uncertain ones.
- **Contested rejections**: when ops argues a rejection and moves it to
  "additional docs required", tag it so both the true rejection rate and the
  docs count stay honest.

### Reference data still needed

- Visa MCC list (for the MCC recommender, phase 7)
- Restricted category and MCC list (phase 7)
- A redacted bank template, even just column headers (phase 6)
- Two or three redacted bank replies in their original messy shapes (phase 4,
  highest value item)

## The dashboard (phase 3)

```bash
python export_dashboard.py     # writes dashboard_data.json
# then inline it into dashboard.html at the __DATA__ placeholder
```

`dashboard.html` carries a `__DATA__` placeholder where the JSON is inlined.
Build and publish the combined file; the data stays out of source control.

What it shows, and why each choice was made:

- **Find a merchant.** Search by legal name and get every acquirer and every
  use case for that merchant side by side, grouped by MCC, with a roll-up
  that says procured everywhere, partially procured, or nothing yet. This is
  the question ops is asked constantly ("where is this merchant?") and it is
  the one view a request-level list loses. The old one-row-per-merchant sheet
  answered it; one row per request does not, until you put the rows back
  together. Grouping by MCC first is what stops two use cases being read as
  one merchant. The page opens on a worked example rather than an empty box.

- **Blended ops TAT as the headline.** Hands-on time only. The requests still
  sitting unraised are excluded from it and reported in their own callout
  underneath, because a blended average that includes an open queue moves with
  backlog size rather than with how ops is performing.
- **The acquirer scorecard.** Volume, ops days, bank days, worst response
  round against the agreed SLA, breach rate, rounds per decision and rejection
  rate. Sorted by bank days, so the laggard surfaces on its own.
- **Ops days and bank days as two separate bars**, never slices of one whole,
  because the clocks overlap.
- **Bounce and rejection reasons** come from the reason codes and the
  classified free text, so these panels build themselves.
- **The exception list** ranks past-SLA first, then repeat bounces, then the
  oldest unraised requests. Any row opens its timeline.
- **The drill-down timeline** lays every episode on one shared date scale with
  the overlaps visible, and states elapsed days against summed party days so
  the gap between them is explicit.

Filters for stream, segment and acquirer sit in one sticky row and drive
everything on the page.


## The parser (phase 4)

`python parse_inbox.py` reads `data/emails/` and scores itself against
`data/email_truth.csv`, which sits outside the inbox so the parser never sees
it. Current figures: 1,179 rows from 590 emails, 98.7% applied automatically,
100% precision on what it applied, 1.3% held for review.

Four decisions worth keeping:

- **Extraction is driven by the shape of the email, not the acquirer.** Going
  by acquirer would mean a bank that changes its layout silently yields zero
  rows and nobody notices until a week of TATs are wrong. Sniffing the shape
  means an unknown layout fails loudly instead.
- **Negations are checked before anything else.** "Not Approved" contains
  "Approved". A substring test stops the wrong clock and records a declined
  merchant as won.
- **MCC is part of the match key, not a tiebreak.** A merchant with two use
  cases has two live applications at the same acquirer. Matching on name alone
  stops the wrong one.
- **Ambiguity fails closed.** Two candidates within 0.04 of each other go to a
  person. A wrong auto-applied row writes a wrong event into the clock and
  every figure downstream inherits it, with nothing flagged.

Two bugs this phase caught, both worth remembering:

- `normalise()` was stripping "India" from legal names, which merged
  "Oakhurst Ventures India Private Limited" with "Oakhurst Ventures Pvt Ltd"
  and matched replies to the wrong request **at 1.00 confidence**. Over-
  normalising destroys distinctions that matter. Entity suffixes are safe to
  canonicalise, including misspelled ones, because they are not the
  distinctive part of a name; anything before them is not.
- The generator was clamping events past the as-of date back to as-of, which
  stacked several events onto one day and produced a merchant appearing twice
  in one reply with two different outcomes. Events past as-of are now dropped
  and the request is left open, which is what "has not happened yet" means.

A note on the 98.7%: that measures the parser against messiness this project
invented. Real inboxes hold cases not modelled here. The generator seeds a
small share of genuinely unparseable rows (wordings outside the vocabulary,
badly mangled names) specifically so the review path is exercised rather than
assumed to work, and the parser declines all of them rather than guessing.

Keyword scoring is used instead of a language model so the demo runs offline
and every decision can be explained. The tail it cannot place is where a model
would earn its keep in production, and the review queue is exactly that tail.

## The intake check (phase 5)

```bash
python run_intake.py           # checks every merchant use case, writes data/intake_report.txt
```

Current figures across 378 merchant use cases: 82.3% ready for every acquirer
on arrival, 9.0% ready for some and blocked for others, 8.7% blocked
everywhere. 45 blank fields, 13 filled in but malformed, 15 filled in, valid,
and contradicting another field on the record.

Design decisions worth keeping:

- **Readiness is per acquirer.** A missing signatory PAN blocks the acquirers
  that ask for it and leaves the rest untouched. One yes-or-no for the merchant
  would hold every application back for a field one bank wanted. The 9% partial
  cases are the TAT this buys.
- **The check runs on arrival, not at template-filling time.** Same gaps, found
  several ops days earlier, so the bounce happens before ops time is spent
  rather than after.
- **Three levels, and the third is the one that earns its place.** Present,
  well formed, and consistent with the rest of the record. A GSTIN carries the
  entity's PAN at characters 3 to 12 and its issuing state in the first two, so
  a record can pass every field-level check and still describe two different
  companies. That contradiction is what an underwriter spots, and it costs a
  full acquirer cycle to find that way.
- **One bounce per party, not per acquirer.** A field blocking three acquirers
  goes back once, naming all three. Bouncing per acquirer would send the same
  problem to the same team three times and start three clocks for it.
- **The comment names the consequence.** "Company GST: not provided (blocks
  ALDRIDGE, NORTHWAY_CYBS, NORTHWAY_FSS)" tells the receiving team how urgent
  it is; "missing GST" does not.
- **No silent routing default.** A required field with no bounce reason mapped
  raises rather than defaulting to BD. Adding a required field means deciding
  who owns it, and a wrong default sends tickets to a team that cannot act.

Two bugs this phase caught:

- MCC was being read off the merchant record, where it does not live. It
  belongs to the request, because one merchant can run several use cases with
  different MCCs. The check now runs per use case.
- A hand-written GSTIN in the test fixtures had a wrong check digit, so every
  test using it would have passed for the wrong reason. There is now a test
  whose only job is to validate the fixture itself.


## The MCC catalogue and eligibility (phase 7)

42 invented MCCs in a nine-category tree, five risk levels, instrument
restrictions and licence requirements, plus six acquirer policies.

**The finding this phase is built on: there is no single restricted category
list.** There are as many as there are acquirers, they do not agree with each
other, and they are not even in the same format. Four shapes turn up:

- a list of MCC codes flagged prohibited or restricted
- the same, split by instrument, so a code is open on debit and closed on credit
- the same, plus a separate and narrower ban list for one product (EMI)
- a page of prose written as business categories, with no codes at all

Nine of the 42 codes produce a disagreement across the six acquirers, and three
split three ways. Nutraceuticals (7428) is the demo case: prohibited at one
acquirer, conditional at two, eligible at three. A single
restricted-category flag on the merchant record would have to pick one of
those answers and be wrong for the rest.

Decisions worth keeping:

- **The strictest source wins, and acquirer policy can only narrow.** If the
  catalogue refuses a code, no acquirer policy makes it eligible.
- **A prose policy answer carries lower confidence and says so.** Matching
  "alcohol" in a paragraph against a category tree is a judgement, not a
  lookup, and recording it at 1.0 would pass it off as a published code list.
- **Four verdicts, not two.** Eligible, conditional, prohibited, unknown. A
  conditional still goes out, with the condition stated in the covering email.
  An unknown goes to a person and is never quietly treated as acceptable.
- **A refusal and an unresolved policy are different.** A refusal means no
  document fixes it, so no bounce is raised: chasing BD for a PAN at a bank
  that will not take the category starts a clock that can never finish. An
  unresolved policy still needs the documents, because the answer may come back
  yes and the chase would then start from scratch.
- **Eligibility keys on the MCC of the request, never one on the merchant
  record.** The same mistake as the phase 5 bug, in a new place: a
  merchant-level MCC is at best one of several use cases.
- **Instrument-level outcomes are not category refusals.** The business is
  acceptable and the rail is not, and the merchant can still go live on the
  rails that are open. Only every requested instrument being barred is a
  prohibition.

Two bugs this phase caught:

- The invented policies started out too similar, and only 1 of 42 codes
  diverged across acquirers, which lost the entire finding. Diversifying the
  breadth of each policy took it to 9.
- Rent collection was marked as refused outright *and* given instrument
  restrictions, which is self-contradictory. There is now a test that a refused
  code cannot also carry instrument rules.
- The generator was drawing MCCs from a hand-written list of real published
  codes while the catalogue used invented ones, so every eligibility answer in
  the dataset came back unresolved while looking fine. The two pools are now
  derived from the catalogue, so they cannot disagree.

## Bank file generation (phase 6)

```bash
python make_batch.py           # writes data/outbox/ and data/batch_report.txt
```

One template per acquirer, described as config in `config/templates.py`. The
shapes follow the real article: five tabs on the largest, an identity block
repeated in two of them, a dropdown reference sheet, paired type-and-value
columns for a rate card, a CSV for one acquirer, four different date formats,
and one acquirer that wants the GST state code where the others want the state
name.

Current run: 408 requests waiting, 325 can go out, seven files built across six
acquirers with the drafts beside them.

Decisions worth keeping:

- **Every column declares who owns it.** Filled from the record, derived,
  constant, ops input, or owned by sales or finance. Most of the largest
  template is not ops', and that is the honest answer to how much of it can be
  automated. The pre-flight report then names the team a blank is waiting on,
  which is a message that can be sent; "17 empty cells" is not.
- **Nothing is invented to fill a cell.** A missing settlement cycle stays
  missing. A plausible value typed in by ops is a commercial term nobody agreed
  and it goes live.
- **A derived column with no rule raises.** A template is config and the rules
  are code, so they can drift. Failing at build time costs a minute; a blank in
  a mandatory cell costs a refused file several days later, with nothing to
  indicate which rule was never written.
- **Sheet grain is declared, not assumed.** The identity tabs are one row per
  merchant; the deal tab is one row per application. A merchant with two use
  cases belongs on the first once and the second twice. Writing it twice on the
  identity tab asks the bank to open two accounts for one merchant, and what
  comes back is two customer ids to unpick later.
- **The repeated identity block is written once.** Both tabs come from one
  resolved row, so they cannot disagree. Filled by hand they routinely do, and
  that check is in the pre-flight anyway as proof the file is internally
  consistent.
- **Identifier columns are written as text.** A pincode starting with zero, a
  twelve-digit account number and a four-digit MCC are all numbers to a
  spreadsheet, and stored as numbers the bank reads something other than what
  was reviewed.
- **Dropdowns are written as real validation, from the same domains the check
  uses**, so the sheet and the rules cannot drift apart.
- **Strict and loose dropdowns get different answers.** One bank's upload
  refuses the file for an off-list value; another takes it and queries it a week
  later. The first is a blocker, the second a warning.
- **Rows are held back, not the file.** Four merchants waiting on a settlement
  cycle do not keep the other fifty out of the morning batch. Same reasoning as
  partial raising at intake: the next slot is tomorrow and the clock on the good
  rows keeps running.
- **A row cap is split, never truncated.** A file one row over the cap is
  refused in full, and the quiet version of that bug drops the overflow with
  nobody knowing which merchants went.
- **Conditional columns state their condition.** A licence reference is needed
  by the merchants whose category requires one, not by all fifty. Reported on
  all fifty it buries every finding that matters.
- **The system drafts, a person sends.** One deliberate checkpoint at the
  external boundary, and a test whose only job is to assert that nothing in
  this phase can send anything.

One bug this phase caught, and it is the useful kind: a stale inbox. The
dataset builder wrote emails into `data/emails/` without clearing it, so a
rebuild left the previous run's replies in place. The parser then scored itself
against an inbox that was part current dataset and part fiction, and its review
rate jumped from 1.0% to 18.2% with nothing wrong in the parser at all. Any
generated-data directory needs clearing before it is refilled.


## The restriction registry (phase 7a)

```bash
python review_restrictions.py          # the registry, the queue, override outcomes
python manage_restrictions.py check 7428 --instruments CC,DC
python manage_restrictions.py add ALDRIDGE mcc 7428 --source rm_call \
    --reason "RM says they have paused this category" --by n.juneja
python manage_restrictions.py candidates
```

Phase 7 encoded the six acquirer policies in code and treated an acquirer's
refusal as a block. That was wrong in a specific and expensive way, and this
phase changes it.

**A block fails invisibly.** These lists change without notice, so the moment
one goes stale the system quietly stops raising a merchant to a bank that would
now accept it. Nobody finds out, because there is no rejection to look at. A
warning fails in front of a person, who either gets the MID or gets a
rejection, and both are visible.

**The list has to be editable by the team that learns the news.** A restriction
list maintained by engineers lags by a sprint, and ops works around a list that
lags. The registry is a JSON file ops edits through a CLI, and an entry takes
effect on the next batch with no release.

### What changed

- **Three enforcement levels instead of a boolean.** A *block* stops the
  request and suppresses the document chase. A *warning* raises it only on a
  recorded decision. A *note* goes out as normal with the condition stated.
- **Only our own policy blocks.** A category the business does not onboard is a
  rule. A category one bank currently refuses is a fact about today, and facts
  about today go stale without telling anyone. Catalogue-level refusals and
  house policy block; every seeded acquirer entry is a warning or a note.
- **Confidence follows the source, not the person entering it.** A published
  circular is 1.0, a written confirmation 0.95, a rejection we actually
  received 0.9, and something said on a call 0.6. Whoever adds an entry is the
  least neutral judge of how solid their own information is.
- **Anything ops adds defaults to a warning.** An entry typed in haste should
  flag tomorrow's batch, not silently remove an acquirer from every batch until
  somebody audits the volumes.
- **Nothing is deleted.** An entry that no longer applies is retired with a
  date and a reason. The question asked afterwards is always when a bank
  changed its position and on whose word.
- **Prose policy is stored at category scope.** It was written as business
  categories, so transcribing it into individual codes would lose that, and the
  next code added to the category would silently fall outside the rule.
- **The flag explains itself.** Not "restricted category" but the entry, its
  source, its date, who recorded it, its confidence, and how previous overrides
  of that same entry turned out.

### Overrides, which is the half that pays for the rest

Letting ops raise against a restriction is only an improvement if the choice is
recorded, because the record is what makes the list self-correcting:

- An override the bank **refuses on the category** proves the entry right.
- An override the bank **approves** proves the entry stale.
- An override refused for an unrelated reason (a broken payment flow, a
  document discrepancy) proves nothing either way and is counted as neither.
  Without that third case the registry grows on evidence that has nothing to do
  with it.

The override is written to the request's event log and to the override log at
the same time, because they answer different questions: the event log says what
this request did, the override log says whether the entry deserves to exist.
The event is an annotation, so it moves no clock and changes no status.

Evidence is read cautiously. One approval is "possibly stale, watch it", not
proof: a single application can slip through underwriting for reasons nobody
records. Three approvals and no category refusals is "looks stale, consider
retiring". Nothing retires automatically.

The headline number is the share of answered overrides the banks refused. Low,
and the warnings are over-cautious and ops is right to push. High, and ops is
spending response rounds on applications that were never going to land, and
those entries should probably become blocks.

### House policy candidates

What every acquirer refuses is computed and shown, never applied. The
intersection of the lists gets wider the fewer lists we hold, so inferring a
block from it would make our rules strongest exactly where our data is
thinnest. Every candidate carries its coverage, unanimity across two lists is
"watch" and across four is "promote", and promotion is a deliberate action
recorded in the change log.

### Current figures

89 live entries across six acquirers plus three house blocks. Of 408 requests
in the queue: 365 clear, 18 clear with conditions, 12 awaiting an ops decision,
13 blocked by our own policy. Replaying the requests the banks have already
answered gives 15 overrides, 54% refused on the category, four entries the
evidence supports and one that looks stale.

One bug this phase caught, and it is a good lesson in why reports get read
side by side with their source: the house policy seed had its reason strings
one row out of step with the codes, so the registry said "adult services" about
the money transfer code. Nothing looked wrong until the report was printed
next to the catalogue. The reasons are now built from the catalogue entry
rather than typed beside the code.


## The SLA chaser (phase 8)

```bash
python run_chaser.py           # writes data/chase_report.txt and data/outbox_chases/
```

Current run: 119 applications sitting with an acquirer, 36 past the agreed
response time, 572 waiting on us and therefore not chased at all.

**Post-live website monitoring was cut from this phase.** It needs to fetch
live merchant sites, which this project has no honest way to test: a scanner
proven against invented web pages proves nothing. It is also a different
product from procurement. Building it here would have padded the project
rather than finished it.

### What the chaser refuses to do

- **Chase the wrong party.** 572 of the open requests are waiting on us, not on
  a bank. A request late because an acquirer asked for a document we have not
  sent is not theirs to answer for, and chasing it spends credibility needed
  for the ones that genuinely are late. Who owes the next move is derived from
  the event log, not from a status field someone remembers to update.
- **Chase on the wrong clock.** Lateness is the round the bank is holding right
  now, never the total. A bank that answered twice quickly and is two days into
  a third round is two days late, not twelve, and a chase it can disprove makes
  the next one unreadable.
- **Chase everything equally.** Days over, weighted by priority tier. The
  weights are steep enough that a P0 one day late outranks a P4 six days late,
  which is the call ops makes by hand anyway, and shallow enough that a P4
  twelve days over still comes above that P0, so the queue does not starve its
  own tail. Both of those are tests.
- **Send the same email forever.** One nudge, one firmer follow-up naming the
  consequence, then it stops emailing and goes on a list for a person to raise
  with the relationship manager. A two-day cooldown on top. Automation that
  escalates to a human at a defined point is credible; automation that keeps
  sending is a robot arguing with a mail filter.
- **Send at all.** Drafts land in `data/outbox_chases/` and a person sends
  them, the same checkpoint as the bank files, with a test that asserts nothing
  in the module can send.

One email per acquirer rather than one per request, for the same reason bounces
are consolidated per party. Every line quotes the date the application went in,
the round, days held and the agreed number, because the only chase a bank
cannot argue with is one that quotes its own commitment back. Each draft also
invites the correction: if it is actually waiting on us, we want to be told.

### Measuring whether chasing works, which is where this got interesting

The naive comparison says chasing makes banks **slower**: chased rounds average
9.5 working days against 4.4 for unchased. That is not an effect, it is the
selection. Every chased round had already run past the SLA; the unchased group
is mostly banks that answered on day two.

Comparing across acquirers does not rescue it either. The chase goes out the
day after each bank's own SLA, so on any given day the chased group is the
banks with short SLAs and the unchased group is the banks with long ones. The
first version of the report did exactly this and showed four banks with zero
chases, which is what exposed it.

Two fixes, and the second is the real one:

1. **Compare inside each acquirer, on that acquirer's own chase day.** Same
   bank, same clock, both arms already past that bank's SLA.
2. **A holdout.** The chase rule is deterministic, so within any one acquirer
   *every* eligible round was chased and there was no control group at all. The
   effect was unmeasurable by construction. So 10% of eligible rounds are
   deliberately never chased, chosen by a stable hash of the request id, with
   P0 merchants excluded because the experiment is not worth running on the
   accounts someone outside ops is already asking about.

The cost is stated rather than hidden: a handful of merchants wait slightly
longer, which is the price of knowing whether the other ninety percent of the
chasing does anything.

And the current answer is **no answer**, which the report says in those words.
125 chased rounds against 9 held out is nowhere near enough, the largest
stratum is 16 short of a twenty-a-side floor, and a chaser that printed a
number from that would be publishing noise a team would then act on. The report
also says plainly that nothing in this dataset was ever actually chased, so
what it demonstrates is the method, not a result about real banks.

There is a test that plants a known two-day effect and checks the estimator
recovers it, which is what makes the rest of the module worth believing.
