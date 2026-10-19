# MID Procurement Workflow

Automating cards MID procurement for a payment aggregator, end to end: intake
checks, per-acquirer category rules, bank submission files, parsed bank
replies, and turnaround time reported per party.

**Live dashboard: https://workflowmidprocurement.vercel.app/**

**[Read the PRD](PRD.md)** for the problem, the rules that are easy to get wrong, what was deliberately not built, and what is not being claimed.

Every acquirer, merchant, policy and template here is invented. The figures
come from a seeded synthetic dataset, not live traffic. No real institution,
merchant, rate card or template appears anywhere in this repository.

---

## The problem

A payment aggregator cannot put a merchant live until an acquiring bank issues
a MID. Each merchant needs its own MID from each acquirer and is raised to
three or four of six or seven in parallel. Around a thousand requests a month,
handled by three people, tracked in a spreadsheet updated by hand.

Three things the spreadsheet cannot do, and this does:

**Report turnaround honestly.** The clocks overlap. Ops runs alongside the
acquirer, because getting the MID is ops' job, and onboarding runs alongside BD
because onboarding owns the ticket until it comes back clean. So per-party days
add up to more than the elapsed time, which means a share-of-delay pie chart
would be arithmetically wrong no matter how good it looked. Nothing here stores
a duration: every figure is derived from an immutable event log on demand, so a
number is never stale and any number can be opened up when somebody asks why a
request took 22 days.

**Hold the real unit of work.** A request is one merchant, one MCC, one
acquirer. One merchant can run several use cases, each needing its own MCC and
its own application, and a tracker with one row per merchant cannot represent
that. It is why bank replies are matched on legal name *plus* MCC.

**Find the gaps on arrival.** Thirty to forty percent of requests bounce back
for missing or contradictory data. Today that surfaces days later, while
somebody is filling in a bank template, after the ops time has already been
spent.

## What is in here

| | |
|---|---|
| **TAT engine** | Replays events into per-party episodes. Working days, per-acquirer holiday calendars, a 2pm cut-off on the acquirer clock only, overlapping clocks, ops time split into solo and overlap, SLA judged per response round rather than on total held time |
| **Synthetic dataset** | ~360 merchants, ~1,380 requests, ~4,600 events and ~650 bank reply emails in each acquirer's own messy format. Seeded, so a demo shows the same numbers twice |
| **Dashboard** | One self-contained HTML page: blended ops TAT, an acquirer scorecard, bounce and rejection reasons, an exception list, and a per-request timeline with the overlaps visible |
| **Bank reply parser** | Two streams, shape-driven extraction, fuzzy matching on legal name and MCC with confidence scoring. 99.0% applied automatically at 100% precision; the rest go to a review queue rather than being guessed |
| **Intake check** | Per-acquirer readiness. Presence, format, and consistency, including the PAN-to-GST cross-check that catches a record describing two different companies. One consolidated bounce per party, naming the acquirers each gap is holding up |
| **MCC catalogue** | 42 categories, nine super categories, five risk levels, instrument restrictions and licence requirements |
| **Restriction registry** | Per-acquirer category rules as editable data with three enforcement levels, source-based confidence, a change log, and recorded overrides whose outcomes say whether each entry still deserves to exist |
| **Bank file generation** | One submission template per acquirer as config, pre-flight checks, and a draft covering email a person sends |

251 tests. Each one is a domain rule written down so a later change cannot
quietly break it.

## Running it

```bash
cd system
pip install -r requirements.txt

python -m pytest tests/ -q      # 251 tests
python example.py               # one merchant, two acquirers, figures to check by hand
python build_dataset.py         # writes the synthetic dataset to data/
python parse_inbox.py           # parses the inbox and scores itself against truth
python run_intake.py            # per-acquirer readiness and category assessment
python make_batch.py            # builds today's bank files and draft emails
python review_restrictions.py   # the registry, today's queue, override outcomes
python export_dashboard.py      # writes dashboard_data.json

python manage_restrictions.py check 7428 --instruments CC,DC
```

Nothing in `data/` is committed. It all rebuilds from the scripts, and a
dataset in the repo would eventually disagree with the code that made it.

## Decisions worth the argument

**Only our own policy blocks.** A category the business will not onboard is a
rule. A category one bank currently refuses is a fact about today, and facts
about today go stale without telling anyone. So an acquirer's refusal raises a
flag and waits for a recorded decision instead of silently removing that bank
from every future batch, which is a failure nobody would ever notice: there is
no rejection to look at.

**The override is the feature, not the exception.** Letting ops raise against a
restriction only helps if the decision is recorded, because the record is what
makes the list self-correcting. An override the bank refuses on the category
proves the entry right. One the bank approves proves it stale. One refused for
an unrelated reason proves nothing either way and is counted as neither.

**Nothing is invented to fill a cell.** A bank template column ops does not own
is left empty and reported against the team that owes it. A plausible
settlement cycle typed in by ops is a commercial term nobody agreed, and it
goes live.

**Ambiguity fails closed.** Two parser candidates within 0.04 of each other go
to a person. A wrong row applied automatically writes a wrong event into the
clock, and every figure downstream inherits it with nothing flagged.

**The system drafts, a person sends.** One deliberate checkpoint at the
external boundary, with a test whose only job is to assert that nothing in the
file generation phase can send anything.

`system/CLAUDE.md` holds the full set, the domain rules that are easy to get
wrong, and the bugs each phase caught.

## Deployment

`index.html` at the root is the deployed dashboard, self-contained with its
data inlined. Vercel serves it as a static site; `system/` is excluded from the
deployment and has no build step.
