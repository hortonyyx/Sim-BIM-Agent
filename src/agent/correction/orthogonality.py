"""Shared orthogonality classification for production and the judge (W5 / R-4).

Production (correction) and the judge share ONE orthogonality yardstick so they
cannot disagree about whether a near-axis edge is legal -- the structural root
of the three false-red rounds, where the judge used its own exactness ceiling to
convict legal upstream geometry of being illegal.  Two questions are kept apart:

  * "is this geometry LEGAL?"            -> authority is the PRODUCTION validator
    (correction).  It rejects a genuinely non-orthogonal edge.
  * "can the judge MEASURE this?"        -> authority is the JUDGE.  It may only
    answer ``unsupported`` (capability NA) for a shape it cannot pair, and must
    NEVER declare upstream geometry broken.  A near-axis edge the production
    validator admitted is paired by the judge's general-seam path, not rejected.

Tightening the epsilon is two-stage (R-4): this batch only ADVISES on
near-orthogonal edges; flipping it to blocking waits for two real runs with
zero advisory hits.

Invariant #4: this module imports nothing from the judge package
(the ``src/agent/judge`` directory; zero gt import) so production and gate(1)
never pull the judge in transitively.
"""
from __future__ import annotations

from typing import Literal

# The frozen orthogonality epsilon shared by production and the judge.  An edge
# is axis-aligned iff dx <= epsilon OR dy <= epsilon.
ORTHOGONALITY_EPSILON = 1e-9

OrthogonalityClass = Literal["axis_aligned", "near_orthogonal_advisory", "non_orthogonal"]


def classify_edge_orthogonality(dx: float, dy: float, *, epsilon: float = ORTHOGONALITY_EPSILON) -> OrthogonalityClass:
    """Classify one polygon edge's orthogonality on the shared yardstick.

    * ``axis_aligned``: dx or dy is zero (exactly orthogonal -- the judge pairs
      these by exact identity after clustering).
    * ``near_orthogonal_advisory``: dx or dy is in (0, epsilon] -- legal but
      worth recording; production keeps admitting it (R-4 advisory-only stage,
      not yet flipped to blocking).
    * ``non_orthogonal``: both dx and dy exceed epsilon -- production rejects.
    """
    adx, ady = abs(dx), abs(dy)
    if adx <= 0.0 or ady <= 0.0:
        return "axis_aligned"
    if adx <= epsilon or ady <= epsilon:
        return "near_orthogonal_advisory"
    return "non_orthogonal"


def edge_is_axis_aligned(dx: float, dy: float, *, epsilon: float = ORTHOGONALITY_EPSILON) -> bool:
    """Production legality gate: an edge is axis-aligned iff dx or dy <= epsilon.

    This is the exact condition ``cell_geometry`` enforces; centralizing it here
    keeps the judge and production on the same epsilon so the judge can never
    brand a legal near-axis edge as a topology break.
    """
    return abs(dx) <= epsilon or abs(dy) <= epsilon
