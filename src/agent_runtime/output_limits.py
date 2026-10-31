"""Reviewed output defaults with an explicit, auditable small-request exception."""

from .estimation import get_model_profile


def default_output_tokens(model: str, *, fallback: int = 2048) -> int:
    profile = get_model_profile(model)
    return profile.recommended_min_output_tokens or fallback


def validate_output_limit(model: str, output_tokens: int, *, reason: str | None = None) -> dict:
    if type(output_tokens) is not int or output_tokens <= 0:
        raise ValueError("explicit positive output token cap required")
    if reason is not None and (not isinstance(reason, str) or not reason.strip()):
        raise ValueError("low output limit override requires a non-empty reason")
    profile = get_model_profile(model)
    minimum = profile.recommended_min_output_tokens
    below = minimum is not None and output_tokens < minimum
    if below and reason is None:
        raise ValueError(
            f"{profile.canonical_name} output cap {output_tokens} is below the recommended "
            f"minimum {minimum}. Basis: {profile.output_limit_source}. "
            "Use low_output_limit_reason / --low-output-limit-reason to record an explicit exception."
        )
    return {"model": profile.canonical_name, "output_tokens": output_tokens,
            "recommended_min_output_tokens": minimum, "basis": profile.output_limit_source,
            "below_recommendation": below, "override_reason": reason}
