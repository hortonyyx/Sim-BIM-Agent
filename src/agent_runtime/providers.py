"""Explicit provider parameters; subscription routes never fall back to billing."""

from pathlib import Path


GLM_SUBSCRIPTION = "glm-subscription"
GLM_SUBSCRIPTION_MODEL = "glm-5.3-flash"
GLM_SUBSCRIPTION_BASE_URL = "https://open.bigmodel.cn/api/coding/paas/v4"
GLM_SUBSCRIPTION_ANTHROPIC = "glm-subscription-anthropic"
GLM_ANTHROPIC_BASE_URL = "https://open.bigmodel.cn/api/anthropic"
CHATGPT_SUBSCRIPTION = "chatgpt-subscription"
CHATGPT_BASE_URL = "https://api.openai.com/v1"
OPENCODE_GO = "opencode-go"
DEEPSEEK = "deepseek"
GLM_SUBSCRIPTION_PROVIDERS = (GLM_SUBSCRIPTION, GLM_SUBSCRIPTION_ANTHROPIC)
SUBSCRIPTION_PROVIDERS = (*GLM_SUBSCRIPTION_PROVIDERS, CHATGPT_SUBSCRIPTION, OPENCODE_GO)
LIVE_PROVIDERS = ("paratera", DEEPSEEK, *SUBSCRIPTION_PROVIDERS)
# Verified 10-03 on the Coding Plan endpoint: omitted behaves like the top level, low/medium cut
# thinking 2-4x (migration_comparison/subscription_effort_calibration.json). Claude Code sends
# output_config.effort=medium with adaptive thinking (evidence/claude_code_request_capture/).
SUBSCRIPTION_REASONING_EFFORTS = ("low", "medium", "high", "max")


def validate_provider_model(provider: str, model: str) -> None:
    if provider == OPENCODE_GO:
        from .opencode_go import model_contract
        model_contract(model)
    if provider == DEEPSEEK and model != "deepseek-flash":
        raise ValueError("reviewed DeepSeek vision route requires model deepseek-flash")
    if isinstance(model, str) and "deepseek" in model.casefold() and provider != DEEPSEEK:
        raise ValueError("DeepSeek requires its explicit reviewed deepseek provider")
    if provider in GLM_SUBSCRIPTION_PROVIDERS and model != GLM_SUBSCRIPTION_MODEL:
        raise ValueError(f"{provider} requires model glm-5.3-flash")
    if provider == CHATGPT_SUBSCRIPTION and (not isinstance(model, str) or not model.strip()):
        raise ValueError("ChatGPT subscription requires an explicit account-available model slug")


def subscription_credentials(path: Path | None = None, *, provider=GLM_SUBSCRIPTION) -> tuple[str, str]:
    """No environment fallback, interpolation, sourcing, or credential logging."""
    if path is None:
        raise ValueError("GLM subscription requires an explicit credentials file")
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError("GLM subscription credentials file does not exist")
    from dotenv import dotenv_values
    private = dotenv_values(path, interpolate=False)
    if provider not in GLM_SUBSCRIPTION_PROVIDERS:
        raise ValueError("not a reviewed GLM subscription provider")
    variable, expected = (("GLM_ANTHROPIC_BASE_URL", GLM_ANTHROPIC_BASE_URL)
        if provider == GLM_SUBSCRIPTION_ANTHROPIC else ("GLM_BASE_URL", GLM_SUBSCRIPTION_BASE_URL))
    base_url, key = private.get(variable), private.get("GLM_API_KEY")
    if base_url != expected:
        raise ValueError(f"{variable} must be the reviewed Coding Plan endpoint")
    if not key:
        raise ValueError("GLM_API_KEY is missing from the specified credentials file")
    return base_url, key


def provider_parameters(provider: str, *, output_tokens: int,
                        temperature: float | None = None,
                        thinking: bool = True, reasoning_effort: str | None = None,
                        model: str | None = None) -> dict:
    if provider == DEEPSEEK:
        validate_provider_model(provider, model)
        if type(output_tokens) is not int or not 0 < output_tokens <= 393216:
            raise ValueError("DeepSeek output_tokens must be within 1..393216")
        parameters = {"max_tokens": output_tokens,
                      "thinking": {"type": "enabled" if thinking else "disabled"}}
        if thinking:
            if temperature is not None:
                raise ValueError("DeepSeek thinking ignores temperature; omit it")
            effort = reasoning_effort or "high"
            if effort not in {"low", "high", "max"}:
                raise ValueError("DeepSeek reasoning_effort must be low/high/max")
            parameters["reasoning_effort"] = effort
        else:
            if reasoning_effort is not None:
                raise ValueError("DeepSeek reasoning_effort requires thinking")
            if temperature is not None:
                parameters["temperature"] = temperature
        return parameters
    if provider == OPENCODE_GO:
        from .opencode_go import parameters
        return parameters(model, output_tokens=output_tokens, temperature=temperature,
                          thinking=thinking, reasoning_effort=reasoning_effort)
    if provider == "paratera" and isinstance(model, str):
        # Original-vendor contracts, separately accepted by Paratera in the
        # 2026-10-22 image/tool preflight. Acceptance does not attest effort.
        name = model.casefold()
        if name in {"kimi-k3", "qwen3.8-max"}:
            if thinking is not True:
                raise ValueError("reviewed Paratera flagship route requires thinking")
            kimi = name == "kimi-k3"
            maximum = 1_048_576 if kimi else 131_072
            if type(output_tokens) is not int or not 0 < output_tokens <= maximum:
                raise ValueError(f"{model} output_tokens must be within 1..{maximum}")
            efforts = {"low", "high", "max"} if kimi else {"low", "medium", "xhigh"}
            effort = reasoning_effort or ("max" if kimi else "xhigh")
            if effort not in efforts:
                raise ValueError("unreviewed Paratera reasoning_effort for " + model)
            if kimi and temperature not in {None, 1.0}:
                raise ValueError("Kimi-K3 requires its original-vendor temperature 1.0")
            # Kimi: platform.kimi.ai/docs/guide/kimi-k3-quickstart
            # Qwen Chat: docs.qwencloud.com/api-reference/chat/openai-chat
            # Qwen max_tokens limits the final answer; native effort governs
            # thinking. Do not reuse the Go Messages combined-cap semantics.
            output_key = "max_completion_tokens" if kimi else "max_tokens"
            return {output_key: output_tokens, "reasoning_effort": effort,
                    "temperature": (1.0 if kimi else 0.6) if temperature is None else temperature}
    parameters = {"max_tokens": output_tokens}
    if provider == CHATGPT_SUBSCRIPTION:
        if temperature is not None or thinking is not True:
            raise ValueError("ChatGPT subscription uses service sampling defaults; select reasoning_effort instead")
        if reasoning_effort is not None:
            if reasoning_effort not in {"low", "medium", "high", "xhigh", "max"}:
                raise ValueError("reviewed ChatGPT models require reasoning_effort low/medium/high/xhigh/max")
            parameters["reasoning_effort"] = reasoning_effort
        # SIWC does not accept max_output_tokens. This is a local reservation
        # allowance only; the Responses adapter must not transmit it as a cap.
        return parameters
    if provider == GLM_SUBSCRIPTION_ANTHROPIC:
        if thinking is not True:
            raise ValueError("GLM-5.3-Flash thinking cannot be disabled")
        effort = reasoning_effort or "medium"
        if effort not in SUBSCRIPTION_REASONING_EFFORTS:
            raise ValueError("unverified subscription reasoning_effort: " + str(effort))
        if temperature is not None:
            raise ValueError("Anthropic adaptive route uses captured service sampling defaults")
        return {**parameters, "thinking": {"type": "adaptive", "display": "omitted"},
            "output_config": {"effort": effort},
            "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}}
    if provider == GLM_SUBSCRIPTION:
        if thinking is not True:
            raise ValueError("GLM-5.3-Flash thinking cannot be disabled; omit the thinking switch")
        if reasoning_effort is not None:
            if reasoning_effort not in SUBSCRIPTION_REASONING_EFFORTS:
                raise ValueError("unverified subscription reasoning_effort: " + str(reasoning_effort))
            parameters["reasoning_effort"] = reasoning_effort
        if temperature is not None:
            parameters["temperature"] = temperature
        return parameters
    if reasoning_effort == "medium":
        raise ValueError("Paratera does not offer reasoning_effort=medium")
    parameters["temperature"] = 0.0 if temperature is None else temperature
    if reasoning_effort:
        parameters["reasoning_effort"] = reasoning_effort
    else:
        parameters["enable_thinking"] = thinking
    return parameters
