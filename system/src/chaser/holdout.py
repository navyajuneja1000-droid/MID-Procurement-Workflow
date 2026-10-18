"""
A slice of eligible requests that deliberately does not get chased.

This exists because of a problem found by trying to measure the chaser and
failing. The chase rule is deterministic: a request is chased on the working
day after its acquirer's SLA passes. That means within any one acquirer,
*every* round that ran long was chased. There is no comparison group, so no
amount of analysis can say what the chasing did. Everyone eligible was treated.

Comparing across acquirers does not rescue it. The chase day is a fixed
function of each bank's SLA, so on any given day the chased group is the banks
with short SLAs and the unchased group is the banks with long ones. Any
difference found that way is bank mix, not chasing.

The only way to answer the question is to not chase some of them on purpose.
So a fixed share of eligible rounds is held out, chosen by a hash of the
request id, which has three properties worth having:

- **Stable.** The same request is always on the same side, so a round does not
  flip arms between runs and pollute the comparison.
- **Unrelated to anything that predicts the outcome.** Not by tier, not by
  acquirer, not by age, so the two arms differ only in whether a chase went.
- **Reproducible without storing anything**, which means the comparison can be
  recomputed from scratch on any dataset.

The cost is real and worth stating: a small number of merchants wait slightly
longer than they would have. That is the price of knowing whether the other
ninety percent of the chasing is doing anything, and it is a price worth paying
exactly once, until the answer is known.

P0 merchants are never held out. The experiment is not worth running on the
accounts where someone outside ops is already asking about the go-live date.
"""

from __future__ import annotations

import hashlib

from ..model import PriorityTier

# One in ten. Small enough that the cost is a handful of merchants, large
# enough that a few hundred rounds give the comparison something to work with.
HOLDOUT_SHARE = 0.10


def in_holdout(
    request_id: str,
    tier: PriorityTier | None = None,
    share: float = HOLDOUT_SHARE,
) -> bool:
    """Is this request in the deliberately-unchased slice?"""
    if tier is PriorityTier.P0:
        return False
    digest = hashlib.sha256(request_id.encode()).hexdigest()
    # The first four hex characters give 65,536 buckets, which is finer than
    # any share anyone would set.
    bucket = int(digest[:4], 16) / 0x10000
    return bucket < share
