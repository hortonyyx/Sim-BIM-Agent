"""Two-end space contracts for 2-D affine transforms (B4-(1)).

Why this module exists
----------------------
``gt_manifest.Affine2D`` is six bare floats plus a non-singularity check.  The
*same* type, and in two cases the *same field name*, carries three different
domain/codomain pairs:

===================================  ==============================  ==========================
field                                 owner                           domain -> codomain
===================================  ==============================  ==========================
``pixel_to_source_m``                 ``RasterOverlayIntentV3`` /     ``pixel -> source_metre``
                                      ``RasterOverlayBindingV1``
``world_from_source_m``               ``PlanViewIntentV1``            ``dxf_native -> world_metre``
``world_from_source_m``               ``PlanViewBindingV1``           ``source_metre -> world_metre``
===================================  ==============================  ==========================

The last two differ by exactly one factor of ``metres_per_unit`` (0.001 for
both sm24 and sm25), i.e. **1000x**, and until this module existed the only
thing keeping them apart was a prose comment in ``tarch_normalize._build_manifest``
plus one hand-written division.  Verified on the real signed sm24 anchor:
the request affine is ``m00 = m11 = 0.001`` and the manifest affine is
``m00 = m11 = 1.0`` with byte-identical translations.

The contract is read through :func:`affine_spaces`, which accepts two carriers:

* **wire fields** (``domain_space`` / ``codomain_space``) for types whose two
  ends genuinely vary by call site -- ``gt_manifest.Affine2D``;
* **class-level declaration** (``DOMAIN_SPACE`` / ``CODOMAIN_SPACE`` class
  attributes) for types whose two ends are a single compile-time constant --
  ``score_schema.Affine2DV1``, which is built at exactly one site and applied
  at exactly one site, always ``reading_plan_local_metre -> world_metre``.
  Declaring those as class attributes keeps them out of ``model_dump`` and so
  leaves ``PlanFrameCertificateV1.preimage_sha256`` byte-identical.

The numeric half of the contract (B4-(2)a)
------------------------------------------
Declarations alone are stamped **by slot and blind to content**: hand an owner
model six bare coefficients with no ``domain_space``/``codomain_space`` and
:func:`bind_affine_spaces` will happily stamp them with whatever that slot is
supposed to carry.  That is not hypothetical -- migration-era signed artifacts
carry exactly that shape (it is why the strip helper exists), and
``tarch_normalize._build_manifest`` still constructs bare ``Affine2D`` /
``Affine1D`` values with a hand-written ``/ metres_per_unit``.  Moving the
manifest affine into the request slot therefore passed silently and put the
sm24 clip corner 12264.66 m away from the truth.

:func:`require_affine_magnitudes` closes that with a check the declaration
cannot fake, because it reads the *coefficients*: once the two ends are known,
the owning document's own ``metres_per_unit`` fixes how much the affine's
linear part is allowed to stretch.

* ``dxf_native`` measures ``metres_per_unit`` metres per unit;
* ``source_metre`` / ``world_metre`` / ``reading_plan_local_metre`` measure 1;
* ``pixel`` measures *nothing the document knows* -- pixel size is a property
  of the raster -- so affines touching it are explicitly not predicted.

For a 2-D affine the prediction is on ``|det|`` and for a 1-D affine on
``|scale|``, so the check is invariant under rotation, reflection and
translation: a drawing turned by an arbitrary angle is never mis-flagged.
Measured on every request/manifest in the repository (127 affine slots), the
relative deviation from the prediction is exactly ``0.0``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterator, Literal, get_args

#: Every space a 2-D affine in this repo may start or end in.
AffineSpace = Literal[
    "pixel",
    "dxf_native",
    "source_metre",
    "world_metre",
    "reading_plan_local_metre",
]

AFFINE_SPACES: tuple[str, ...] = get_args(AffineSpace)

#: Keys the wire carrier uses.  Named once so the hash-stripping helper and the
#: models cannot drift apart.
AFFINE_SPACE_KEYS: tuple[str, str] = ("domain_space", "codomain_space")


class AffineSpaceError(ValueError):
    """Base for every two-end space contract failure."""


class AffineSpaceUndeclared(AffineSpaceError):
    """The affine carries neither wire fields nor a class-level declaration."""


class AffineSpaceMismatch(AffineSpaceError):
    """The affine's declared two ends are not the ones the call site requires."""


class AffineMagnitudeMismatch(AffineSpaceError):
    """The affine's coefficients contradict the two ends it declares.

    Raised by :func:`require_affine_magnitude`.  Separate from
    :class:`AffineSpaceMismatch` because the failure is the opposite direction:
    the declaration is well-formed, the *numbers* are from another space.
    """


def _coerce_space(value: object, *, what: str) -> str:
    if not isinstance(value, str) or value not in AFFINE_SPACES:
        raise AffineSpaceError(f"{what} is not a known affine space: {value!r}")
    return value


def affine_spaces(affine: Any) -> tuple[str, str]:
    """Return ``(domain_space, codomain_space)`` for *affine*.

    Wire fields win when both are populated; otherwise the class-level
    declaration is used.  An affine that declares neither raises
    :class:`AffineSpaceUndeclared` -- silence is never read as "any space".
    """
    domain = getattr(affine, "domain_space", None)
    codomain = getattr(affine, "codomain_space", None)
    if domain is None or codomain is None:
        domain = getattr(type(affine), "DOMAIN_SPACE", None)
        codomain = getattr(type(affine), "CODOMAIN_SPACE", None)
    if domain is None or codomain is None:
        raise AffineSpaceUndeclared(
            f"{type(affine).__name__} declares no domain/codomain space; "
            "bind it at the owning model before use"
        )
    return (
        _coerce_space(domain, what="domain_space"),
        _coerce_space(codomain, what="codomain_space"),
    )


def require_affine_spaces(
    affine: Any,
    *,
    domain: str,
    codomain: str,
    context: str = "affine",
) -> Any:
    """Fail loudly unless *affine* declares exactly ``domain -> codomain``."""
    _coerce_space(domain, what="required domain")
    _coerce_space(codomain, what="required codomain")
    actual_domain, actual_codomain = affine_spaces(affine)
    if (actual_domain, actual_codomain) != (domain, codomain):
        raise AffineSpaceMismatch(
            f"{context}: requires {domain} -> {codomain} but the affine declares "
            f"{actual_domain} -> {actual_codomain}"
        )
    return affine


def affine_coefficients(affine: Any) -> tuple[float, float, float, float, float, float]:
    """Read ``(xx, xy, x0, yx, yy, y0)`` from either coefficient naming."""
    if hasattr(affine, "m00"):
        return (
            float(affine.m00), float(affine.m01), float(affine.m02),
            float(affine.m10), float(affine.m11), float(affine.m12),
        )
    if hasattr(affine, "xx"):
        return (
            float(affine.xx), float(affine.xy), float(affine.x0),
            float(affine.yx), float(affine.yy), float(affine.y0),
        )
    raise AffineSpaceError(
        f"{type(affine).__name__} carries no recognised affine coefficients"
    )


@dataclass(frozen=True)
class ComposedAffine:
    """The result of :func:`compose_affine`.

    Deliberately not one of the wire models: composition crosses model
    families, so nothing here should pick a home class (and so nothing here can
    accidentally be written into a hash-bound payload).
    """

    xx: float
    xy: float
    x0: float
    yx: float
    yy: float
    y0: float
    domain_space: str
    codomain_space: str

    def apply(self, point: tuple[float, float]) -> tuple[float, float]:
        return (
            self.xx * point[0] + self.xy * point[1] + self.x0,
            self.yx * point[0] + self.yy * point[1] + self.y0,
        )


def compose_affine(left: Any, right: Any, *, context: str = "compose_affine") -> ComposedAffine:
    """Compose ``left`` then ``right`` (i.e. the map ``right . left``).

    Reads left-to-right in space terms: composing ``pixel -> source_metre`` with
    ``source_metre -> world_metre`` yields ``pixel -> world_metre``.  When
    ``left``'s codomain is not ``right``'s domain the call raises
    :class:`AffineSpaceMismatch` -- the whole point of the helper.
    """
    left_domain, left_codomain = affine_spaces(left)
    right_domain, right_codomain = affine_spaces(right)
    if left_codomain != right_domain:
        raise AffineSpaceMismatch(
            f"{context}: cannot chain {left_domain} -> {left_codomain} into "
            f"{right_domain} -> {right_codomain} ({left_codomain} != {right_domain})"
        )
    a_xx, a_xy, a_x0, a_yx, a_yy, a_y0 = affine_coefficients(left)
    b_xx, b_xy, b_x0, b_yx, b_yy, b_y0 = affine_coefficients(right)
    return ComposedAffine(
        xx=b_xx * a_xx + b_xy * a_yx,
        xy=b_xx * a_xy + b_xy * a_yy,
        x0=b_xx * a_x0 + b_xy * a_y0 + b_x0,
        yx=b_yx * a_xx + b_yy * a_yx,
        yy=b_yx * a_xy + b_yy * a_yy,
        y0=b_yx * a_x0 + b_yy * a_y0 + b_y0,
        domain_space=left_domain,
        codomain_space=right_codomain,
    )


# --------------------------------------------------------------------------- #
# The numeric half of the contract: coefficients must agree with the two ends
# --------------------------------------------------------------------------- #

#: Relative tolerance for the magnitude prediction.
#:
#: NOT fitted to the fixtures.  The floor is double-precision noise on a
#: two-term product of coefficients (~1e-16 relative) plus whatever a rotation
#: costs (``cos^2 + sin^2`` is 1 to within ~1e-16), so 1e-9 leaves seven orders
#: of headroom for honest arithmetic.  The ceiling is the hazard being caught,
#: which is a whole factor of ``metres_per_unit`` (1e3 per axis, 1e6 on ``|det|``
#: for sm24/sm25) -- fifteen orders above this tolerance.  There is no value in
#: between that a real drawing occupies.
AFFINE_MAGNITUDE_REL_TOL: float = 1e-9


def space_unit_metres(space: str, *, metres_per_unit: float) -> float | None:
    """How many metres one unit of *space* measures for this document.

    ``None`` means the document does not fix a metric for that space -- there is
    exactly one such space, ``pixel``, whose size belongs to the raster and not
    to the drawing.  Returning ``None`` rather than 1.0 is deliberate: silence
    must not be read as "unit scale" any more than an undeclared affine may be
    read as "any space".
    """
    space = _coerce_space(space, what="space")
    if space == "pixel":
        return None
    if space == "dxf_native":
        return float(metres_per_unit)
    return 1.0


def affine_magnitude(affine: Any) -> tuple[float, str]:
    """Return ``(value, kind)`` -- ``|det|`` for a 2-D affine, ``|scale|`` for 1-D."""
    if hasattr(affine, "scale") and hasattr(affine, "source_axis"):
        return abs(float(affine.scale)), "abs_scale"
    xx, xy, _x0, yx, yy, _y0 = affine_coefficients(affine)
    return abs(xx * yy - xy * yx), "abs_det"


def expected_affine_magnitude(
    affine: Any, *, metres_per_unit: float
) -> tuple[float, str] | None:
    """Predicted ``(value, kind)`` from the declared two ends, or ``None``.

    ``None`` is returned only when one end is a space the document assigns no
    metric to (``pixel``).  An affine that declares no ends at all raises
    :class:`AffineSpaceUndeclared` here, exactly as everywhere else.
    """
    domain, codomain = affine_spaces(affine)
    domain_unit = space_unit_metres(domain, metres_per_unit=metres_per_unit)
    codomain_unit = space_unit_metres(codomain, metres_per_unit=metres_per_unit)
    if domain_unit is None or codomain_unit is None:
        return None
    gain = domain_unit / codomain_unit
    _actual, kind = affine_magnitude(affine)
    return ((gain * gain) if kind == "abs_det" else gain), kind


def require_affine_magnitude(
    affine: Any, *, metres_per_unit: float, context: str = "affine"
) -> Any:
    """Fail loudly when the coefficients contradict the declared two ends.

    This is the half of the contract that bare coefficients cannot slip past:
    the declaration is stamped by slot and is blind to content, but ``|det|``
    (resp. ``|scale|``) is read off the numbers themselves.
    """
    prediction = expected_affine_magnitude(affine, metres_per_unit=metres_per_unit)
    if prediction is None:
        return affine
    expected, kind = prediction
    actual, _kind = affine_magnitude(affine)
    if not math.isclose(actual, expected, rel_tol=AFFINE_MAGNITUDE_REL_TOL, abs_tol=0.0):
        domain, codomain = affine_spaces(affine)
        ratio = (actual / expected) if expected else float("inf")
        raise AffineMagnitudeMismatch(
            f"{context}: an affine declaring {domain} -> {codomain} in a document with "
            f"metres_per_unit={metres_per_unit!r} must have {kind}={expected!r}, but its "
            f"coefficients give {kind}={actual!r} (off by a factor of {ratio:.6g})"
        )
    return affine


def _is_affine_like(value: Any) -> bool:
    """Duck-test for 'this object is an affine carrying a two-end contract'.

    Structural rather than a type list on purpose: this module is imported *by*
    the model modules, so it cannot name their classes, and a hard-coded list is
    precisely the thing that rots when a fifth affine slot is added.
    """
    if hasattr(value, "scale") and hasattr(value, "source_axis"):
        return True
    return (hasattr(value, "m00") and hasattr(value, "m11")) or (
        hasattr(value, "xx") and hasattr(value, "yy")
    )


def iter_affines(model: Any, *, path: str = "") -> Iterator[tuple[str, Any]]:
    """Yield ``(dotted_path, affine)`` for every affine anywhere under *model*.

    Walks pydantic models, lists and tuples.  Discovery is by shape, so a newly
    added affine field is covered the day it is added rather than the day
    somebody remembers to extend a list.
    """
    if _is_affine_like(model):
        yield (path or type(model).__name__, model)
        return
    if isinstance(model, (list, tuple)):
        for index, item in enumerate(model):
            yield from iter_affines(item, path=f"{path}[{index}]")
        return
    fields = getattr(type(model), "model_fields", None)
    if not isinstance(fields, dict):
        return
    for name in fields:
        child = getattr(model, name, None)
        if child is None or isinstance(child, (str, bytes, int, float, bool)):
            continue
        yield from iter_affines(child, path=f"{path}.{name}" if path else name)


def require_affine_magnitudes(
    model: Any, *, metres_per_unit: float, context: str
) -> Any:
    """Run :func:`require_affine_magnitude` over every affine under *model*.

    Called from the root model that owns ``metres_per_unit`` -- the only place
    that knows the document's native scale.  Because discovery is structural,
    an affine field added without an owning ``bind_affine_spaces`` call does not
    quietly escape the gate: it reaches here undeclared and raises
    :class:`AffineSpaceUndeclared`.
    """
    for where, affine in iter_affines(model):
        require_affine_magnitude(
            affine, metres_per_unit=metres_per_unit, context=f"{context}.{where}"
        )
    return model


def strip_affine_space_keys(value: Any) -> Any:
    """Recursively drop the space-contract keys from a JSON-ready payload.

    Used by the canonical hash preimages so that adding the contract does not
    invalidate already-signed requests/manifests.  See the migration note on
    ``compute_request_sha256`` / ``canonical_manifest_payload``.
    """
    if isinstance(value, dict):
        return {
            key: strip_affine_space_keys(item)
            for key, item in value.items()
            if key not in AFFINE_SPACE_KEYS
        }
    if isinstance(value, list):
        return [strip_affine_space_keys(item) for item in value]
    return value


def bind_affine_spaces(model: Any, field: str, *, domain: str, codomain: str) -> None:
    """Stamp (or verify) the two-end spaces of ``model.<field>`` in place.

    Called from the owning model's ``model_validator``: the owner is the only
    place that knows which pair of spaces its affine field connects.

    * undeclared affine -> stamped with ``domain -> codomain``;
    * already declared and matching -> left alone;
    * already declared and *different* -> :class:`AffineSpaceMismatch`.

    The third branch is the one with teeth: a manifest ``world_from_source_m``
    (``source_metre -> world_metre``) handed to ``PlanViewIntentV1`` (which needs
    ``dxf_native -> world_metre``) is rejected instead of being silently used
    1000x off.
    """
    _coerce_space(domain, what="bound domain")
    _coerce_space(codomain, what="bound codomain")
    affine = getattr(model, field)
    declared = (
        getattr(affine, "domain_space", None),
        getattr(affine, "codomain_space", None),
    )
    if declared == (None, None):
        object.__setattr__(
            model,
            field,
            affine.model_copy(update={"domain_space": domain, "codomain_space": codomain}),
        )
        return
    if declared != (domain, codomain):
        raise AffineSpaceMismatch(
            f"{type(model).__name__}.{field} requires {domain} -> {codomain} but the "
            f"affine declares {declared[0]} -> {declared[1]}"
        )


__all__ = [
    "AFFINE_MAGNITUDE_REL_TOL",
    "AFFINE_SPACES",
    "AFFINE_SPACE_KEYS",
    "AffineMagnitudeMismatch",
    "AffineSpace",
    "AffineSpaceError",
    "AffineSpaceMismatch",
    "AffineSpaceUndeclared",
    "ComposedAffine",
    "affine_coefficients",
    "affine_magnitude",
    "affine_spaces",
    "bind_affine_spaces",
    "compose_affine",
    "expected_affine_magnitude",
    "iter_affines",
    "require_affine_magnitude",
    "require_affine_magnitudes",
    "require_affine_spaces",
    "space_unit_metres",
    "strip_affine_space_keys",
]
