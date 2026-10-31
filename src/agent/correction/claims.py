"""Opening-claim vocabulary — the shared, single-source word list every C2
consumer (view manifest opening_evidence, WindowV3.provenance keys, Va
applicability, B4b per-claim denominator) validates against.

Per the upstream E2' evidence matrix (c2_full_unlock_design.md §E2'): a window
opening's identity claim ("does it exist") is separate from its individual
attribute claims ("along position", "sill height", ...). Each channel (plan /
elevation) can independently attest a subset of these seven claims; the vocab
here is exhaustive — no consumer may invent an eighth claim string.

Content owner: B2 detail spec §2.8 (this module is created by the B-M batch
per the r4 cross-batch dependency ruling — B-M lands first, B2 consumes it —
so its literal content is authoritative from that section, not invented here).
"""

from __future__ import annotations

CLAIM_EXISTENCE = "existence"
CLAIM_HOST = "host"
CLAIM_ALONG = "along"
CLAIM_WIDTH = "width"
CLAIM_SILL = "sill"
CLAIM_HEAD = "head"
CLAIM_APPEARANCE = "appearance"

WINDOW_CLAIMS: frozenset[str] = frozenset(
    {
        CLAIM_EXISTENCE,
        CLAIM_HOST,
        CLAIM_ALONG,
        CLAIM_WIDTH,
        CLAIM_SILL,
        CLAIM_HEAD,
        CLAIM_APPEARANCE,
    }
)

CLAIMS_VOCAB_VERSION = "1"

# Per E2' matrix: what each evidence channel can potentially attest, before any
# per-opening applicability narrowing. Plan carries no z-height (sill/head) or
# appearance evidence; elevation carries no host-wall evidence.
PLAN_POTENTIALLY_OBSERVABLE_CLAIMS: frozenset[str] = frozenset(
    {CLAIM_EXISTENCE, CLAIM_HOST, CLAIM_ALONG, CLAIM_WIDTH}
)
ELEVATION_POTENTIALLY_OBSERVABLE_CLAIMS: frozenset[str] = frozenset(
    {CLAIM_EXISTENCE, CLAIM_ALONG, CLAIM_WIDTH, CLAIM_SILL, CLAIM_HEAD, CLAIM_APPEARANCE}
)

__all__ = [
    "CLAIM_EXISTENCE",
    "CLAIM_HOST",
    "CLAIM_ALONG",
    "CLAIM_WIDTH",
    "CLAIM_SILL",
    "CLAIM_HEAD",
    "CLAIM_APPEARANCE",
    "WINDOW_CLAIMS",
    "CLAIMS_VOCAB_VERSION",
    "PLAN_POTENTIALLY_OBSERVABLE_CLAIMS",
    "ELEVATION_POTENTIALLY_OBSERVABLE_CLAIMS",
]
