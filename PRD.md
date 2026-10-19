# MID Procurement Automation

**Product requirements · v1.0 · October 2026**

Navya Juneja · written from two years running this process at a payment
aggregator, and built to find out which parts of it a system can actually take.

---

## 1. In one page

A payment aggregator cannot put a merchant live until an acquiring bank issues
a MID. Getting that MID is a manual, multi-party chase across six or seven
banks, tracked in a spreadsheet, and nobody in the business can say with
confidence where the time goes.

This product does four things the spreadsheet cannot:

1. **Reports turnaround honestly.** Every figure is derived from an immutable
   event log rather than typed into a cell, and the clocks are allowed to
   overlap because in reality they do.
2. **Finds the gaps on arrival** instead of several ops days later, and bounces
   them once, to the team that owns them, naming what each gap is holding up.
3. **Builds each bank's submission file** from one resolved record, checks it
   for the errors a person cannot see by eye, and drafts the covering email.
4. **Chases what is actually late**, on the round the bank is holding now, and
   stops chasing when a third email would do more harm than good.

The scope deliberately stops at the external boundary. Every outbound email is
drafted and a person sends it.

**Status:** built and tested end to end against a seeded synthetic dataset of
360 merchants and 1,376 requests. 283 tests. No real merchant, bank, rate card
or template appears anywhere in it, and it has never run against a live
pipeline. Section 11 is explicit about what that means.

---

## 2. The problem

### 2.1 What has to happen before a merchant can take a card payment

A merchant signs with the aggregator. Before a single transaction can be
processed, each acquiring bank the merchant will route through has to issue a
MID against that merchant's specific line of business. One merchant commonly
needs three or four MIDs from a panel of six or seven acquirers, raised in
parallel, each with its own forms, its own category rules and its own idea of
how long a reply should take.

Every one of those applications passes between four parties: BD, who owns the
merchant relationship and the documents; onboarding, who owns the ticket;
operations, who raises it and chases it; and the acquirer, who decides.

### 2.2 The scale this runs at

| | |
|---|---|
| Requests a month | ~1,000 |
| People handling them | 3 |
| Acquirers on the panel | 6 to 7 |
| Acquirers per merchant | 3 to 4, in parallel |
| Requests that bounce back for missing or wrong data | 30 to 40% |
| Where it is tracked | one spreadsheet, updated by hand |

### 2.3 Four failures, in the order they cost money

**Turnaround is unknowable.** Leadership asks how long procurement takes. The
answer is produced by hand each week by reading a sheet of dates. It cannot be
broken down by party without someone rebuilding it, it cannot be opened up when
a specific merchant took 22 days, and because the sheet records a status rather
than a history, a cell that gets overwritten takes the evidence with it.

**Gaps are found at the worst possible moment.** A missing signatory PAN
surfaces while someone is filling in a bank's template, which is several ops
days after the ticket arrived. The ticket then bounces, and those days are
spent twice: once before the bounce and once after it comes back.

**Each bank is a different animal, and the differences are undocumented.** Six
acquirers means six submission templates, six sets of category rules, six
definitions of a reasonable reply time, and six reply formats to read. All of
that knowledge lives in the heads of three people.

**Chasing is either forgotten or indiscriminate.** There is no list of what is
genuinely late, so follow-up is either skipped or done by sending everyone a
reminder, which trains banks to ignore them.

### 2.4 Why the spreadsheet cannot be fixed by being a better spreadsheet

The tracker holds one row per merchant. The actual unit of work is **one
merchant, one MCC, one acquirer**: a merchant running two lines of business
needs a separate application per line, per bank, each with its own outcome.
The moment a merchant has two use cases, a one-row-per-merchant sheet has to
either lose one of them or describe both in a cell, and every report built on
top inherits that.

---

## 3. Who this is for

| User | What they need | What they get today |
|---|---|---|
| **Ops analyst** (3 people, primary user) | What can go out this morning, what is blocked and on whom, what is late | A sheet they maintain by hand and a memory of which bank wants what |
| **Ops lead** | Where time goes, which acquirer is slow, whether the backlog is growing | A weekly figure rebuilt by hand, with no way to open it up |
| **BD** | What is missing on their merchant, once, clearly, with the consequence | Several separate messages, often repeating the same gap |
| **Onboarding** | The same, for the fields they own | The same problem |
| **Leadership** | One honest turnaround number per party | A number they cannot interrogate |

The ops analyst is the primary user and every design call resolves in their
favour. A feature that produces a better report but costs the analyst a
keystroke a day is the wrong trade.

---

## 4. Goals and non-goals

### 4.1 Goals

1. **Every turnaround figure derived, never stored.** Any number can be opened
   up to the events that produced it.
2. **Find and route every gap on arrival**, in one consolidated bounce per
   party.
3. **Generate each bank's file** from a single resolved record, with the errors
   a human cannot see caught before it goes.
4. **Hold the per-acquirer knowledge as data**, editable by the team that
   learns it, not as code or as memory.
5. **Chase on evidence**, and be able to say whether the chasing works.

### 4.2 Non-goals, each one deliberate

- **It does not send anything.** One checkpoint at the external boundary,
  because the cost of a wrong file reaching a bank is not a retry, it is a
  conversation with a relationship manager and a batch that misses its slot.
- **It does not decide eligibility on its own.** A bank's restricted-category
  list is a fact about today. The system surfaces it and a person decides, for
  reasons set out in section 6.4.
- **It does not replace the acquirer's own systems**, integrate with core
  banking, or attempt to predict a bank's decision.
- **It does not monitor merchant websites after go-live.** Cut deliberately;
  see section 9.
- **It does not do automated matching without confidence controls.** A wrongly
  matched bank reply writes a wrong event into the clock, and every figure
  downstream inherits it with nothing flagged.

---

## 5. The rules that are easy to get wrong

This section is the reason the product works, and it is the part that cannot be
derived from first principles by someone who has not run the process. Each rule
is encoded as an executable test.

**5.1 The unit of work is merchant + MCC + acquirer.** Not merchant + acquirer.
Bank replies are therefore matched on legal name *plus* MCC, because a merchant
with two live applications at one bank has two different answers coming.

**5.2 Clocks overlap, in two specific places.** Ops runs alongside the acquirer,
because getting the MID is ops' job and they do not put it down while the bank
has it. Onboarding runs alongside BD, because onboarding owns the ticket until
it comes back clean. The consequence: per-party days sum to more than elapsed
time, so **a share-of-delay pie chart is arithmetically wrong no matter how
good it looks**. The honest visual is a timeline per request with the overlaps
drawn.

**5.3 Ops is measured per episode, not merged.** A request ops picks up three
times is three entries, and that count is itself the rework measure. Raising to
a bank splits an episode into solo and overlap phases but is not a new pickup.

**5.4 Ops time splits into solo and overlap.** Solo is the efficiency figure.
Overlap is time ops is accountable for but does not control. Reporting one
without the other misleads in opposite directions.

**5.5 A 2pm cut-off, on the acquirer clock only.** A batch sent after 2pm starts
the bank's clock the next working day. Internal clocks start immediately.

**5.6 Working days, with per-acquirer holiday calendars.** Most acquirers share
one; one or two differ.

**5.7 Same-day handoff counts as one day, not zero.** A party that handled and
passed on a request consumed a day of someone's attention.

**5.8 Deprioritisation stops every clock**, and those requests leave the TAT
averages but are counted separately with how long they sat first. That is ops
effort which produced nothing, and hiding it flatters the team.

**5.9 Approval and MID delivery are different events on different email
threads.** The gap between them is invisible in the tracker and sits inside
procurement turnaround. Measured here as a separate acquirer phase.

**5.10 Acquirer SLA is judged per response round, not on total held time.** A
bank that answers in 4 days, asks for a document, then answers in 4 more has
met a 5-day SLA twice. Charging it the 8-day sum bills the bank for our own
turnaround in between, and a bank that receives a complaint it can disprove
stops reading the next one. Total held time is still reported, separately, as
a measure of our own data quality: more than one round means the application
could not be decided in a single pass.

**5.11 BAU and bulk migration are reported separately.** A migration backlog
otherwise destroys the BAU averages and hides a real capacity problem behind a
one-off event.

**5.12 The merchant is not a party.** Time waiting on a merchant is charged to
BD, as it is today. Whether that is fair to BD is an open question in section
10.

---

## 6. What the product does

### 6.1 Intake: find the gaps on arrival

Checks every application the moment the ticket lands, at three levels:

1. **Present.** Is the field there.
2. **Well formed.** Does the PAN match the PAN format, does the GSTIN pass its
   own checksum.
3. **Consistent with the rest of the record.** A GSTIN carries the entity's PAN
   at characters 3 to 12 and its issuing state in the first two, so a record
   can pass every field-level check and still describe two different companies.
   That contradiction is what an underwriter spots, and finding it that way
   costs a full acquirer cycle.

Readiness is decided **per acquirer**, because acquirers ask for different
fields. A missing signatory PAN blocks the three that want it and leaves the
fourth untouched, which is what makes partial raising possible.

Bounces are consolidated **one per party, not per acquirer**, and the comment
names the consequence: "Company GST: not provided (blocks ALDRIDGE,
NORTHWAY_CYBS, NORTHWAY_FSS)" tells the receiving team how urgent it is in a
way that "missing GST" does not.

*Current run, 390 merchant use cases: 70.3% clean for every acquirer they were
raised to, 12.6% ready for some and blocked for others, 17.2% blocked
everywhere.*

### 6.2 Bank file generation

One submission template per acquirer, described as configuration rather than
code, because banks change these templates with no notice and no version
number. The cost of absorbing a change decides whether the file is generated or
typed.

Decisions worth defending:

- **Every column declares who owns it**: filled from the record, derived,
  constant, ops input, or owned by sales or finance. Most of the largest
  template is not ops', and that is the honest answer to how much of it can be
  automated. The pre-flight then reports "sales owes a settlement cycle on four
  merchants", which is a message that can be sent. "17 empty cells" is not.
- **Nothing is invented to fill a cell.** A plausible value typed in by ops is
  a commercial term nobody agreed, and it goes live.
- **Sheet grain is declared.** The identity tabs are one row per merchant, the
  deal tab one row per application. Writing a two-use-case merchant twice on
  the identity tab asks the bank to open two accounts for one merchant.
- **Identifier columns are written as text.** A pincode starting with zero and
  a twelve-digit account number are both numbers to a spreadsheet, and stored
  as numbers the bank reads something other than what was reviewed.
- **Rows are held back, not the file.** Four merchants waiting on a missing
  value do not keep the other fifty out of the morning batch.

*Current run: 408 requests waiting, 324 go out today, 59 held on documents, 13
blocked by house policy, 12 waiting on an ops decision about a restriction.*

### 6.3 Bank reply parser

Two streams, status replies and MID deliveries, read from each bank's own messy
format: different column names, free-text status wording, rejection reasons
written as sentences, legal names that drift from the record, and replies that
cover only part of a batch.

- **Extraction is driven by the shape of the email, not the acquirer.** Going
  by acquirer means a bank that changes its layout silently yields zero rows
  and nobody notices for a week.
- **Negations are checked first.** "Not Approved" contains "Approved", and a
  substring test records a declined merchant as won.
- **Ambiguity fails closed.** Two candidates within 0.04 of each other go to a
  person.

*Current run: 1,250 rows from 626 emails, 99.0% applied automatically at 100%
precision, 1.0% held for review.*

### 6.4 The restriction registry

Each acquirer keeps its own restricted-category list. They do not agree with
each other and they are not even in the same format: coded lists, lists split
by payment instrument, a separate narrower ban for one product, and a page of
prose with no codes at all. **A merchant on one category can be eligible at one
bank, conditional at a second and refused at a third**, so this cannot be
answered once for the merchant.

The important design call: **only our own policy blocks.** A category the
business will not onboard is a rule. A category one bank currently refuses is a
fact about today, and facts about today go stale without telling anyone. A
hard block fails invisibly, because the system quietly stops raising to a bank
that would now accept, and there is no rejection to notice. So an acquirer's
refusal raises a flag and waits for a recorded decision.

Every entry carries its source, and confidence follows the source rather than
being typed in by whoever added it: a published circular is 1.0, a written
confirmation 0.95, a rejection actually received 0.9, something said on a call
0.6. Nothing is ever deleted; entries are retired with a date and a reason.

Overrides are recorded, which is what makes the list self-correcting. An
override the bank refuses on the category proves the entry right. One the bank
approves proves it stale. One refused for an unrelated reason proves nothing
either way and is counted as neither.

*Current registry: 89 live entries across six acquirers, 3 blocking (all house
policy), 33 warnings, 53 notes.*

### 6.5 The SLA chaser

Finds what each acquirer is holding past the agreed response time, ranks it,
drafts one email per acquirer, and stops.

- **Nothing is chased that is waiting on us.** *572 of the currently open
  requests are.* A request late because a bank asked for a document nobody sent
  is not theirs to answer for, and chasing it spends credibility needed for the
  ones that genuinely are late.
- **Lateness is the round they hold now** (rule 5.10).
- **Ranked by days over, weighted by priority tier**, steeply enough that a P0
  one day late outranks a P4 six days late, shallowly enough that a P4 twelve
  days over still outranks that P0, so the queue does not starve its own tail.
- **The ladder ends**: one nudge, one firmer follow-up, then it stops emailing
  and writes a note for a person to raise on the relationship call.

*Current run: 119 applications with an acquirer, 36 past the agreed time.*

### 6.6 The dashboard

One self-contained page. Blended ops turnaround as the headline, hands-on time
only, with the unraised queue excluded and reported separately underneath,
because an average that includes an open queue moves with backlog size rather
than with how the team is performing.

Then the acquirer scorecard sorted by bank days so the laggard surfaces on its
own, bounce and rejection reasons built from the reason codes, an exception
list ranked past-SLA first, and a per-request timeline with the overlapping
clocks drawn and elapsed days stated against summed party days, so the gap
between those two numbers is explicit rather than hidden.

---

## 7. How success is measured

### 7.1 What the product is for

| Measure | Why this one | Baseline today |
|---|---|---|
| Ops solo days per request | Hands-on time, the only part the team controls | 2.6 days on the synthetic set |
| Bounces per request | Each one is a round trip that could have been found on arrival | 27% of requests bounce |
| Share found on arrival vs at template time | The gap this closes | not measurable in the old tracker |
| Unraised queue age | The capacity signal the old report could not show | median 22 days, oldest 50 |
| Acquirer breach rate, per round | Holds banks to what they agreed, defensibly | 21% of 792 answered rounds |

### 7.2 The number deliberately not optimised

**Not match rate.** A parser that matches more rows looks better and is worse:
a wrongly matched reply writes a wrong event into the clock and every figure
downstream inherits it silently. The parser is measured on precision of what it
applied, and the review queue is treated as a feature rather than a failure.

### 7.3 What cannot be measured yet, and why that is said out loud

Whether chasing actually speeds banks up **cannot currently be answered**, and
the report says so in those words rather than printing a number.

The naive comparison says chasing makes banks slower (9.5 working days against
4.4). That is selection, not effect: every chased round had already run past
the SLA. Comparing across acquirers does not fix it either, because the chase
day is a fixed function of each bank's SLA, so the chased arm is simply the
banks with short SLAs.

The deeper problem is that the chase rule is deterministic, so within any one
bank every eligible round was chased and there is no control group at all. The
effect is unmeasurable by construction. So 10% of eligible rounds are
deliberately never chased, and the comparison runs inside each acquirer on that
acquirer's own chase day.

At current volume that collects roughly nine held-out rounds against 125
chased, which is far short of a usable floor. The honest answer is therefore
"ask again in a few months", and a system that printed a number from that
evidence would be publishing noise that a team would act on.

---

## 8. Requirements, by capability

Each row is implemented and covered by tests. P0 means the product does not
work without it.

| # | Requirement | Priority |
|---|---|---|
| R1 | Status is derived from an immutable event log, never stored | P0 |
| R2 | Working-day calendar with per-acquirer holidays and a 2pm acquirer cut-off | P0 |
| R3 | Per-party episodes with overlap preserved, ops split into solo and overlap | P0 |
| R4 | Acquirer SLA judged on the worst single response round | P0 |
| R5 | Per-acquirer field requirements as configuration | P0 |
| R6 | Three-level validation: presence, format, cross-field consistency | P0 |
| R7 | One consolidated bounce per party, naming the acquirers each gap blocks | P0 |
| R8 | Partial raising: ready acquirers go today, blocked ones wait | P0 |
| R9 | Per-acquirer submission templates as configuration, with column ownership | P0 |
| R10 | Pre-flight: dropdown domains, duplicate rows, number mangling, row caps, tab consistency | P0 |
| R11 | Draft covering email per batch; no send path exists | P0 |
| R12 | Reply parsing by email shape, with negation handling and confidence scoring | P0 |
| R13 | Ambiguous matches queue for review rather than resolve | P0 |
| R14 | MCC catalogue with risk levels, instrument restrictions and licence requirements | P1 |
| R15 | Editable restriction registry with three enforcement levels and source-based confidence | P1 |
| R16 | Overrides recorded against the entry, with outcome tracking | P1 |
| R17 | Chase list by whose court, on the current round, ranked by tier-weighted lateness | P1 |
| R18 | Escalation ladder with a cooldown, ending in a human handover | P1 |
| R19 | Holdout and within-acquirer comparison for chase effectiveness | P2 |
| R20 | Dashboard: blended TAT, acquirer scorecard, exception list, drill-down timeline | P0 |

---

## 9. What was cut, and why

**Post-live website monitoring.** Originally scoped alongside the chaser. It
requires fetching live merchant sites, which this project has no honest way to
test: a scanner proven against invented web pages proves nothing. It is also a
different product from procurement, closer to a compliance scanner. Building it
here would have padded the project instead of finishing it.

**Automatic promotion of acquirer agreement into house policy.** The system
computes which categories every acquirer refuses and shows them as candidates,
but will not block on them. The intersection of the lists gets wider the fewer
lists we hold, so inferring a rule from it would make the rules strongest
exactly where the data is thinnest. Promotion is a deliberate, recorded human
action.

**A merchant clock.** Time waiting on a merchant is still charged to BD. Adding
a fifth party is modelled but not built, because it changes every historical
figure and needs the operating team to agree first.

**Sending.** Permanently, not for now.

---

## 10. Open questions

1. **Should the merchant be a party?** BD is currently charged for merchant
   silence. Adding a merchant clock would be fairer to BD and would make every
   historical comparison discontinuous.
2. **Were all 117 fields on the largest bank template mandatory**, or does a
   typical merchant fill a fraction? This decides how much of that file can
   truthfully be called automated.
3. **What is the right holdout share?** 10% answers the chase question in
   months. 20% answers it in half the time and makes twice as many merchants
   wait.
4. **Should contested rejections be a separate state?** When ops argues a
   rejection and the bank reclassifies it as "additional documents required",
   both the true rejection rate and the document count should stay honest.
5. **How should the registry handle a bank that loosens a category
   permanently?** Currently a person retires the entry. There is a case for the
   evidence doing it automatically after enough approvals, and a stronger case
   against.

---

## 11. Status, and what is not claimed

**What exists:** a working system of 37 source files and 283 tests, with a
live dashboard, covering intake, file generation, reply parsing, the
restriction registry and the chaser.

**What it runs on:** a seeded synthetic dataset of 360 merchants, 1,376
requests, 4,311 events and 626 bank emails. Acquirers, merchants, templates,
category lists and rate cards are all invented. Nothing from any real workplace
appears in it, by design.

**What is therefore not claimed:**

- No real MID has been procured through this.
- The accuracy figures measure the system against messiness this project
  invented. Real inboxes hold cases not modelled here.
- The turnaround figures describe the synthetic pipeline, not any real team's
  performance.
- The chase effectiveness question is unanswered, and section 7.3 explains why
  rather than estimating around it.

**What production would need:** a real mailbox integration, the real per-bank
templates and category lists loaded into the registry, a few weeks of someone
checking the system's judgement of "late" and "blocked" against their own, and
agreement from the operating team on the open questions in section 10.

---

## 12. Reading the code

| Where | What |
|---|---|
| `system/src/model.py` | Entities, events, bounce routing. Status derived, never stored |
| `system/src/tat.py` | The engine. Replays events into per-party episodes |
| `system/src/calendar_rules.py` | Working days, holidays, the 2pm cut-off |
| `system/src/intake/` | Validation and per-acquirer readiness |
| `system/src/filegen/` | Template spec, mapping, pre-flight, writer, covering email |
| `system/src/parsing/` | Shape-driven extraction, classification, matching |
| `system/src/catalogue/` | MCC catalogue, acquirer policies, registry, overrides |
| `system/src/chaser/` | Overdue detection, ladder, drafts, effect measurement |
| `system/tests/` | 283 tests, each one a rule from section 5 written down |
| `system/CLAUDE.md` | Engineering notes, including every bug each phase caught |
