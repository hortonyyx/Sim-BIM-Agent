"""Provider-reported token totals; no estimates and no invented missing usage."""


def reported_total_tokens(raw) -> int | None:
    if not isinstance(raw, dict):
        return None
    for name in ("total_tokens", "total_token_count"):
        value = raw.get(name)
        if type(value) is int and value >= 0:
            return value
    if "prompt_tokens" in raw:
        fields = ("prompt_tokens", "completion_tokens")
    else:
        # Anthropic input_tokens excludes cache reads/writes. Do not add the
        # nested cache_creation TTL breakdown again to its top-level total.
        fields = ("input_tokens", "output_tokens") + tuple(
            k for k in ("cache_read_input_tokens", "cache_creation_input_tokens") if k in raw)
    values = [raw.get(k) for k in fields]
    if all(type(value) is int and value >= 0 for value in values):
        return sum(values)
    return None
