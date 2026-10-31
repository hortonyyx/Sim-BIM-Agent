"""Exact-rational interval claims and dual-domain conservation ledgers.

Candidate selection remains policy/tolerance based in ``segment_score``.  Once
an observation is registered to one support line, this module turns every
coverage into one source-bearing claim.  Target and observation ledgers consume
the same claim and therefore the same canonical geometry cut ids.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import hashlib
from typing import Iterable, Mapping

from src.agent.judge.score_schema import ScoreContractError


Exact = Fraction
ExactPoint = tuple[Exact, Exact]


def exact_float(value: float) -> Exact:
    """Losslessly lift one finite binary64 leaf into the rational domain."""
    numerator, denominator = float(value).as_integer_ratio()
    return Fraction(numerator, denominator)


def exact_point(point: tuple[float, float]) -> ExactPoint:
    return exact_float(point[0]), exact_float(point[1])


def exact_bytes(value: Exact) -> str:
    return f"{value.numerator}/{value.denominator}"


def exact_point_bytes(point: ExactPoint) -> tuple[str, str]:
    return exact_bytes(point[0]), exact_bytes(point[1])


def _stable_id(prefix: str, payload: object) -> str:
    return prefix + ":" + hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()[:24]


def _dot(left: ExactPoint, right: ExactPoint) -> Exact:
    return left[0] * right[0] + left[1] * right[1]


def _sub(left: ExactPoint, right: ExactPoint) -> ExactPoint:
    return left[0] - right[0], left[1] - right[1]


def _add(left: ExactPoint, right: ExactPoint) -> ExactPoint:
    return left[0] + right[0], left[1] + right[1]


def _scale(vector: ExactPoint, amount: Exact) -> ExactPoint:
    return vector[0] * amount, vector[1] * amount


@dataclass(frozen=True)
class GeometryCutToken:
    cut_id: str
    point_exact: ExactPoint
    endpoint_sources: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class MappedCut:
    geometry_cut_id: str
    target_exact: Exact
    observation_exact: Exact
    endpoint_sources: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class MappingCertificate:
    certificate_id: str
    target_key: str
    observation_key: str
    geometry_cut_ids: tuple[str, str]
    target_values: tuple[Exact, Exact]
    observation_values: tuple[Exact, Exact]


@dataclass(frozen=True)
class CoverageClaim:
    target_key: str
    observation_key: str
    target_interval: tuple[Exact, Exact]
    observation_interval: tuple[Exact, Exact]
    cuts: tuple[MappedCut, MappedCut]
    mapping_certificate: MappingCertificate
    axis_error_m: float
    position_error_m: float


@dataclass
class CanonicalCutRegistry:
    """Request-local cut registry; a shared vertex is evaluated once per obs."""

    geometry: dict[ExactPoint, GeometryCutToken]
    observation_values: dict[tuple[str, str], Exact]
    observation_evaluation_count: dict[tuple[str, str], int]

    @classmethod
    def empty(cls) -> "CanonicalCutRegistry":
        return cls({}, {}, {})

    def geometry_cut(
        self,
        point: ExactPoint,
        *,
        source: tuple[str, str],
    ) -> GeometryCutToken:
        existing = self.geometry.get(point)
        if existing is not None:
            if source in existing.endpoint_sources:
                return existing
            merged = GeometryCutToken(
                existing.cut_id,
                existing.point_exact,
                tuple(sorted((*existing.endpoint_sources, source))),
            )
            self.geometry[point] = merged
            return merged
        token = GeometryCutToken(
            _stable_id("cut", exact_point_bytes(point)),
            point,
            (source,),
        )
        self.geometry[point] = token
        return token

    def observation_value(
        self,
        observation_key: str,
        cut: GeometryCutToken,
        value: Exact,
    ) -> Exact:
        key = observation_key, cut.cut_id
        existing = self.observation_values.get(key)
        if existing is not None:
            if existing != value:
                _raise_denominator(
                    "mapping_certificate_inconsistent",
                    observation=observation_key,
                    cut_id=cut.cut_id,
                    first_exact=exact_bytes(existing),
                    second_exact=exact_bytes(value),
                )
            return existing
        self.observation_values[key] = value
        self.observation_evaluation_count[key] = 1
        return value


@dataclass(frozen=True)
class IntervalAtom:
    lo_exact: Exact
    hi_exact: Exact
    status: str
    target_ids: tuple[str, ...] = ()
    observation_ids: tuple[str, ...] = ()
    lo_cut_ids: tuple[str, ...] = ()
    hi_cut_ids: tuple[str, ...] = ()

    @property
    def measure_exact(self) -> Exact:
        return self.hi_exact - self.lo_exact


@dataclass(frozen=True)
class TargetLedger:
    target_key: str
    domain_exact: Exact
    atoms: tuple[IntervalAtom, ...]
    matched_exact: tuple[tuple[str, Exact], ...]
    miss_exact: Exact
    duplicate_exact: Exact

    @property
    def canonical_bytes(self) -> tuple[object, ...]:
        return (
            self.target_key,
            exact_bytes(self.domain_exact),
            tuple(_atom_bytes(atom) for atom in self.atoms),
            tuple((key, exact_bytes(value)) for key, value in self.matched_exact),
            exact_bytes(self.miss_exact),
            exact_bytes(self.duplicate_exact),
        )


@dataclass(frozen=True)
class ObservationLedger:
    observation_key: str
    domain_exact: Exact
    atoms: tuple[IntervalAtom, ...]
    covered_exact: Exact
    extra_exact: Exact

    @property
    def canonical_bytes(self) -> tuple[object, ...]:
        return (
            self.observation_key,
            exact_bytes(self.domain_exact),
            tuple(_atom_bytes(atom) for atom in self.atoms),
            exact_bytes(self.covered_exact),
            exact_bytes(self.extra_exact),
        )


def _atom_bytes(atom: IntervalAtom) -> tuple[object, ...]:
    return (
        exact_bytes(atom.lo_exact),
        exact_bytes(atom.hi_exact),
        atom.status,
        atom.target_ids,
        atom.observation_ids,
        atom.lo_cut_ids,
        atom.hi_cut_ids,
    )


def _raise_denominator(reason: str, **context: object) -> None:
    raise ScoreContractError(
        "score_denominator_nonconserving",
        "scoring.denominator_totality",
        context={"reason": reason, **context},
    )


def _segment_exact(segment: object) -> tuple[ExactPoint, ExactPoint, Exact, ExactPoint]:
    p1 = exact_point(getattr(segment, "p1"))
    p2 = exact_point(getattr(segment, "p2"))
    length = exact_float(getattr(segment, "length"))
    direction = _sub(p2, p1)
    if direction == (0, 0) or length <= 0:
        _raise_denominator(
            "coverage_claim_zero_domain",
            segment=getattr(segment, "key"),
        )
    return p1, p2, length, direction


def _parameter_on_segment(
    point: ExactPoint,
    *,
    p1: ExactPoint,
    p2: ExactPoint,
    length: Exact,
    direction: ExactPoint,
) -> Exact:
    if point == p1:
        return Fraction(0)
    if point == p2:
        return length
    denominator = _dot(direction, direction)
    return _dot(_sub(point, p1), direction) * length / denominator


def _point_at_parameter(
    parameter: Exact,
    *,
    p1: ExactPoint,
    length: Exact,
    direction: ExactPoint,
) -> ExactPoint:
    return _add(p1, _scale(direction, parameter / length))


def build_coverage_claim(
    *,
    target: object,
    observation: object,
    axis_error_m: float,
    position_error_m: float,
    cut_registry: CanonicalCutRegistry,
) -> CoverageClaim | None:
    """Build both parameter intervals from one exact pair of geometry cuts."""
    target_p1, target_p2, target_length, target_direction = _segment_exact(target)
    obs_p1, obs_p2, obs_length, obs_direction = _segment_exact(observation)
    obs_on_target = (
        _parameter_on_segment(
            obs_p1,
            p1=target_p1,
            p2=target_p2,
            length=target_length,
            direction=target_direction,
        ),
        _parameter_on_segment(
            obs_p2,
            p1=target_p1,
            p2=target_p2,
            length=target_length,
            direction=target_direction,
        ),
    )
    projected_lo, projected_hi = sorted(obs_on_target)
    lo = max(Fraction(0), projected_lo)
    hi = min(target_length, projected_hi)
    if hi <= lo:
        return None

    target_key = str(getattr(target, "key"))
    observation_key = str(getattr(observation, "key"))
    selected: list[MappedCut] = []
    target_bounds = ((lo, "lo"), (hi, "hi"))
    for target_value, role in target_bounds:
        point = _point_at_parameter(
            target_value,
            p1=target_p1,
            length=target_length,
            direction=target_direction,
        )
        if target_value == 0:
            target_source = (target_key, "DOMAIN_START")
        elif target_value == target_length:
            target_source = (target_key, "DOMAIN_END")
        else:
            target_source = (observation_key, f"PROJECTED_{role.upper()}")
        geometry_cut = cut_registry.geometry_cut(point, source=target_source)

        # The monotone affine certificate maps the observation endpoints'
        # exact target parameters to observation-native [0, L].
        obs_start_t, obs_end_t = obs_on_target
        if obs_end_t == obs_start_t:
            _raise_denominator(
                "mapping_certificate_zero_slope",
                target=target_key,
                observation=observation_key,
            )
        observation_value = (
            (target_value - obs_start_t)
            * obs_length
            / (obs_end_t - obs_start_t)
        )
        if observation_value == 0:
            observation_value = Fraction(0)
        elif observation_value == obs_length:
            observation_value = obs_length
        observation_value = cut_registry.observation_value(
            observation_key, geometry_cut, observation_value
        )
        selected.append(
            MappedCut(
                geometry_cut.cut_id,
                target_value,
                observation_value,
                geometry_cut.endpoint_sources,
            )
        )

    observation_values = tuple(sorted(cut.observation_exact for cut in selected))
    certificate_payload = (
        target_key,
        observation_key,
        tuple(cut.geometry_cut_id for cut in selected),
        tuple(exact_bytes(cut.target_exact) for cut in selected),
        tuple(exact_bytes(value) for value in observation_values),
    )
    certificate = MappingCertificate(
        _stable_id("mapping", certificate_payload),
        target_key,
        observation_key,
        tuple(cut.geometry_cut_id for cut in selected),
        (lo, hi),
        observation_values,
    )
    claim = CoverageClaim(
        target_key,
        observation_key,
        (lo, hi),
        observation_values,
        (selected[0], selected[1]),
        certificate,
        float(axis_error_m),
        float(position_error_m),
    )
    return accept_coverage_claim(
        claim,
        target_domain=target_length,
        observation_domain=obs_length,
    )


def accept_coverage_claim(
    claim: CoverageClaim,
    *,
    target_domain: Exact,
    observation_domain: Exact,
) -> CoverageClaim:
    """Validate the two-domain clip and mapping certificate before ledger use."""
    t_lo, t_hi = claim.target_interval
    o_lo, o_hi = claim.observation_interval
    certificate = claim.mapping_certificate
    expected_cut_ids = tuple(cut.geometry_cut_id for cut in claim.cuts)
    expected_obs = tuple(sorted(cut.observation_exact for cut in claim.cuts))
    valid = (
        Fraction(0) <= t_lo < t_hi <= target_domain
        and Fraction(0) <= o_lo < o_hi <= observation_domain
        and tuple(cut.target_exact for cut in claim.cuts) == (t_lo, t_hi)
        and expected_obs == (o_lo, o_hi)
        and certificate.target_key == claim.target_key
        and certificate.observation_key == claim.observation_key
        and certificate.geometry_cut_ids == expected_cut_ids
        and certificate.target_values == (t_lo, t_hi)
        and certificate.observation_values == (o_lo, o_hi)
    )
    if not valid:
        _raise_denominator(
            "coverage_claim_mapping_inconsistent",
            target=claim.target_key,
            observation=claim.observation_key,
            target_interval=tuple(exact_bytes(value) for value in claim.target_interval),
            observation_interval=tuple(
                exact_bytes(value) for value in claim.observation_interval
            ),
            cut_target_values=tuple(
                exact_bytes(cut.target_exact) for cut in claim.cuts
            ),
            cut_observation_values=tuple(
                exact_bytes(cut.observation_exact) for cut in claim.cuts
            ),
            mapping_certificate_id=certificate.certificate_id,
        )
    return claim


def _cuts_for_claims(
    *,
    domain: Exact,
    claims: Iterable[CoverageClaim],
    interval_name: str,
) -> tuple[dict[Exact, set[str]], tuple[CoverageClaim, ...]]:
    materialized = tuple(claims)
    cuts: dict[Exact, set[str]] = {
        Fraction(0): {"DOMAIN_START"},
        domain: {"DOMAIN_END"},
    }
    for claim in materialized:
        if interval_name == "target_interval":
            mapped_cuts = tuple(
                (cut.target_exact, cut) for cut in claim.cuts
            )
        elif interval_name == "observation_interval":
            mapped_cuts = tuple(sorted(
                (
                    (cut.observation_exact, cut)
                    for cut in claim.cuts
                ),
                key=lambda item: item[0],
            ))
        else:  # pragma: no cover - closed internal call surface
            raise ValueError(f"unknown claim interval: {interval_name}")
        if tuple(value for value, _cut in mapped_cuts) != getattr(
            claim, interval_name
        ):
            _raise_denominator(
                "coverage_claim_cut_order_inconsistent",
                target=claim.target_key,
                observation=claim.observation_key,
                interval_name=interval_name,
            )
        for value, cut in mapped_cuts:
            cuts.setdefault(value, set()).add(cut.geometry_cut_id)
    return cuts, materialized


def _check_partition(
    *,
    owner_key: str,
    domain: Exact,
    atoms: tuple[IntervalAtom, ...],
) -> None:
    if not atoms:
        _raise_denominator(
            "interval_ledger_empty",
            owner=owner_key,
            domain_exact=exact_bytes(domain),
        )
    cursor = Fraction(0)
    for atom in atoms:
        if atom.lo_exact != cursor or atom.hi_exact <= atom.lo_exact:
            _raise_denominator(
                "interval_ledger_noncontiguous",
                owner=owner_key,
                expected_lo_exact=exact_bytes(cursor),
                actual_lo_exact=exact_bytes(atom.lo_exact),
                actual_hi_exact=exact_bytes(atom.hi_exact),
                atoms=tuple(_atom_bytes(item) for item in atoms),
            )
        if atom.status not in {"matched", "miss", "duplicate", "covered", "extra"}:
            _raise_denominator(
                "interval_ledger_missing_state",
                owner=owner_key,
                atom=_atom_bytes(atom),
            )
        cursor = atom.hi_exact
    if cursor != domain:
        _raise_denominator(
            "interval_ledger_wrong_domain_end",
            owner=owner_key,
            expected_hi_exact=exact_bytes(domain),
            actual_hi_exact=exact_bytes(cursor),
            atoms=tuple(_atom_bytes(item) for item in atoms),
        )
    if sum((atom.measure_exact for atom in atoms), Fraction(0)) != domain:
        _raise_denominator(
            "interval_ledger_measure_mismatch",
            owner=owner_key,
            domain_exact=exact_bytes(domain),
            atoms=tuple(_atom_bytes(item) for item in atoms),
        )


def build_target_ledger(
    *,
    target_key: str,
    domain_exact: Exact,
    claims: Iterable[CoverageClaim],
) -> TargetLedger:
    cuts, materialized = _cuts_for_claims(
        domain=domain_exact,
        claims=claims,
        interval_name="target_interval",
    )
    values = sorted(cuts)
    atoms: list[IntervalAtom] = []
    matched: dict[str, Exact] = {}
    miss = Fraction(0)
    duplicate = Fraction(0)
    for lo, hi in zip(values, values[1:], strict=False):
        if hi <= lo:
            continue
        owners = tuple(sorted(
            claim.observation_key
            for claim in materialized
            if claim.target_interval[0] <= lo
            and hi <= claim.target_interval[1]
        ))
        if not owners:
            status = "miss"
            miss += hi - lo
        elif len(owners) == 1:
            status = "matched"
            matched[owners[0]] = matched.get(owners[0], Fraction(0)) + hi - lo
        else:
            status = "duplicate"
            duplicate += hi - lo
        atoms.append(IntervalAtom(
            lo,
            hi,
            status,
            observation_ids=owners,
            lo_cut_ids=tuple(sorted(cuts[lo])),
            hi_cut_ids=tuple(sorted(cuts[hi])),
        ))
    result = TargetLedger(
        target_key,
        domain_exact,
        tuple(atoms),
        tuple(sorted(matched.items())),
        miss,
        duplicate,
    )
    check_target_ledger(result)
    return result


def check_target_ledger(ledger: TargetLedger) -> None:
    _check_partition(
        owner_key=ledger.target_key,
        domain=ledger.domain_exact,
        atoms=ledger.atoms,
    )
    matched_by_observation: dict[str, Exact] = {}
    atom_miss = Fraction(0)
    atom_duplicate = Fraction(0)
    for atom in ledger.atoms:
        if atom.status == "matched":
            if len(atom.observation_ids) != 1:
                _raise_denominator(
                    "target_atom_owner_state_mismatch",
                    target=ledger.target_key,
                    atom=_atom_bytes(atom),
                )
            owner = atom.observation_ids[0]
            matched_by_observation[owner] = (
                matched_by_observation.get(owner, Fraction(0))
                + atom.measure_exact
            )
        elif atom.status == "miss":
            atom_miss += atom.measure_exact
        elif atom.status == "duplicate":
            atom_duplicate += atom.measure_exact
    expected_matched = tuple(sorted(matched_by_observation.items()))
    if (
        ledger.matched_exact != expected_matched
        or ledger.miss_exact != atom_miss
        or ledger.duplicate_exact != atom_duplicate
    ):
        _raise_denominator(
            "target_ledger_summary_mismatch",
            target=ledger.target_key,
            matched_exact=tuple(
                (key, exact_bytes(value))
                for key, value in ledger.matched_exact
            ),
            expected_matched_exact=tuple(
                (key, exact_bytes(value))
                for key, value in expected_matched
            ),
            miss_exact=exact_bytes(ledger.miss_exact),
            expected_miss_exact=exact_bytes(atom_miss),
            duplicate_exact=exact_bytes(ledger.duplicate_exact),
            expected_duplicate_exact=exact_bytes(atom_duplicate),
            atoms=tuple(_atom_bytes(atom) for atom in ledger.atoms),
        )
    matched = sum((value for _, value in ledger.matched_exact), Fraction(0))
    if matched + ledger.miss_exact + ledger.duplicate_exact != ledger.domain_exact:
        _raise_denominator(
            "target_subintervals_do_not_tile",
            target=ledger.target_key,
            target_length=float(ledger.domain_exact),
            accounted_length=float(
                matched + ledger.miss_exact + ledger.duplicate_exact
            ),
            target_length_exact=exact_bytes(ledger.domain_exact),
            matched_exact=exact_bytes(matched),
            miss_exact=exact_bytes(ledger.miss_exact),
            duplicate_exact=exact_bytes(ledger.duplicate_exact),
            atoms=tuple(_atom_bytes(atom) for atom in ledger.atoms),
        )


def build_observation_ledger(
    *,
    observation_key: str,
    domain_exact: Exact,
    claims: Iterable[CoverageClaim],
) -> ObservationLedger:
    cuts, materialized = _cuts_for_claims(
        domain=domain_exact,
        claims=claims,
        interval_name="observation_interval",
    )
    values = sorted(cuts)
    atoms: list[IntervalAtom] = []
    covered = Fraction(0)
    extra = Fraction(0)
    for lo, hi in zip(values, values[1:], strict=False):
        if hi <= lo:
            continue
        owners = tuple(sorted(
            claim.target_key
            for claim in materialized
            if claim.observation_interval[0] <= lo
            and hi <= claim.observation_interval[1]
        ))
        if not owners:
            status = "extra"
            extra += hi - lo
        else:
            status = "covered"
            covered += hi - lo
        atom = IntervalAtom(
            lo,
            hi,
            status,
            target_ids=owners,
            lo_cut_ids=tuple(sorted(cuts[lo])),
            hi_cut_ids=tuple(sorted(cuts[hi])),
        )
        atoms.append(atom)
        if len(owners) > 1:
            charged = sum(
                (
                    claim.observation_interval[1]
                    - claim.observation_interval[0]
                    for claim in materialized
                ),
                Fraction(0),
            )
            duplicate_charge = (hi - lo) * (len(owners) - 1)
            _raise_denominator(
                "observation_cover_exceeds_length",
                observation=observation_key,
                obs_length=float(domain_exact),
                covered=float(charged),
                excess=float(duplicate_charge),
                atom_lo_exact=exact_bytes(lo),
                atom_hi_exact=exact_bytes(hi),
                trigger_atom_exact=(
                    exact_bytes(lo),
                    exact_bytes(hi),
                ),
                target_ids=owners,
                multiplicity=len(owners),
                domain_exact=exact_bytes(domain_exact),
                charged_exact=exact_bytes(charged),
                duplicate_charge_exact=exact_bytes(duplicate_charge),
                mapping_certificate_ids=tuple(sorted(
                    claim.mapping_certificate.certificate_id
                    for claim in materialized
                    if claim.target_key in owners
                )),
                atoms=tuple(_atom_bytes(item) for item in atoms),
            )
    result = ObservationLedger(
        observation_key,
        domain_exact,
        tuple(atoms),
        covered,
        extra,
    )
    check_observation_ledger(result)
    return result


def check_observation_ledger(ledger: ObservationLedger) -> None:
    _check_partition(
        owner_key=ledger.observation_key,
        domain=ledger.domain_exact,
        atoms=ledger.atoms,
    )
    atom_covered = sum(
        (
            atom.measure_exact
            for atom in ledger.atoms
            if atom.status == "covered"
        ),
        Fraction(0),
    )
    atom_extra = sum(
        (
            atom.measure_exact
            for atom in ledger.atoms
            if atom.status == "extra"
        ),
        Fraction(0),
    )
    if (
        ledger.covered_exact != atom_covered
        or ledger.extra_exact != atom_extra
    ):
        _raise_denominator(
            "observation_ledger_summary_mismatch",
            observation=ledger.observation_key,
            covered_exact=exact_bytes(ledger.covered_exact),
            expected_covered_exact=exact_bytes(atom_covered),
            extra_exact=exact_bytes(ledger.extra_exact),
            expected_extra_exact=exact_bytes(atom_extra),
            atoms=tuple(_atom_bytes(atom) for atom in ledger.atoms),
        )
    if ledger.covered_exact + ledger.extra_exact != ledger.domain_exact:
        _raise_denominator(
            "observation_atoms_do_not_tile",
            observation=ledger.observation_key,
            domain_exact=exact_bytes(ledger.domain_exact),
            covered_exact=exact_bytes(ledger.covered_exact),
            extra_exact=exact_bytes(ledger.extra_exact),
            atoms=tuple(_atom_bytes(atom) for atom in ledger.atoms),
        )


def claims_by_key(
    claims: Iterable[CoverageClaim],
    attribute: str,
) -> Mapping[str, tuple[CoverageClaim, ...]]:
    grouped: dict[str, list[CoverageClaim]] = {}
    for claim in claims:
        grouped.setdefault(str(getattr(claim, attribute)), []).append(claim)
    return {
        key: tuple(sorted(
            values,
            key=lambda claim: claim.mapping_certificate.certificate_id,
        ))
        for key, values in grouped.items()
    }
