"""
PAN and GSTIN generation and validation.

Real format rules, invented numbers. These matter because the readiness check
in a later phase validates more than presence: a field can be filled and still
be wrong, and a wrong PAN costs a full bank rejection cycle to discover.

PAN:   AAAAA9999A, where the 4th character encodes entity type
       (C company, P individual, F firm or LLP).
GSTIN: 2-digit state code + the entity's PAN + entity number + 'Z' + checksum.
       So characters 3 to 12 of a GSTIN must equal the company PAN, which is
       a cross-check worth running.
"""

from __future__ import annotations

import random
import re
import string

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")

_CHECKSUM_ALPHABET = string.digits + string.ascii_uppercase  # 36 characters


def make_pan(rng: random.Random, entity_type: str = "C") -> str:
    """A format-valid PAN. entity_type sets the 4th character."""
    head = "".join(rng.choice(string.ascii_uppercase) for _ in range(3))
    tail = rng.choice(string.ascii_uppercase)
    digits = f"{rng.randint(0, 9999):04d}"
    last = rng.choice(string.ascii_uppercase)
    return f"{head}{entity_type}{tail}{digits}{last}"


def is_valid_pan(value: str) -> bool:
    return bool(PAN_RE.match(value.strip().upper()))


def gstin_checksum(first_fourteen: str) -> str:
    """The published GSTIN check-digit algorithm.

    Each character's value is its index in 0-9A-Z. Positions alternate a
    factor of 1 and 2, starting at 1. Each product contributes its quotient
    plus remainder on division by 36. The check character closes the sum to
    the next multiple of 36.
    """
    if len(first_fourteen) != 14:
        raise ValueError("GSTIN checksum needs the first 14 characters")

    total = 0
    for i, ch in enumerate(first_fourteen.upper()):
        value = _CHECKSUM_ALPHABET.index(ch)
        factor = 1 if i % 2 == 0 else 2
        product = value * factor
        total += product // 36 + product % 36

    return _CHECKSUM_ALPHABET[(36 - total % 36) % 36]


def make_gstin(rng: random.Random, state_code: str, pan: str) -> str:
    """A GSTIN that is internally consistent with the PAN it is built from."""
    entity_number = rng.choice("123")
    body = f"{state_code}{pan}{entity_number}Z"
    return body + gstin_checksum(body)


def is_valid_gstin(value: str) -> bool:
    v = value.strip().upper()
    if not GSTIN_RE.match(v):
        return False
    return gstin_checksum(v[:14]) == v[14]


def gstin_matches_pan(gstin: str, pan: str) -> bool:
    """Characters 3 to 12 of a GSTIN are the entity's PAN."""
    return gstin.strip().upper()[2:12] == pan.strip().upper()


def corrupt_pan(rng: random.Random, pan: str) -> str:
    """Produce a plausible wrong PAN: the kind of thing someone actually
    types, not random noise. Transposed characters, a digit where a letter
    belongs, or a character short."""
    choice = rng.choice(["transpose", "wrong_type", "short"])
    chars = list(pan)
    if choice == "transpose" and len(chars) > 6:
        i = rng.randint(0, 3)
        chars[i], chars[i + 1] = chars[i + 1], chars[i]
        return "".join(chars)
    if choice == "wrong_type":
        chars[rng.randint(0, 4)] = rng.choice(string.digits)
        return "".join(chars)
    return pan[:-1]
