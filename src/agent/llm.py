import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from omegaconf import OmegaConf

from src.configs.config import LLMConfig

load_dotenv()

# Environment override for the LLM config file. When set (e.g. by
# run_full_pipeline to a per-case `<case>/llm.yaml`), every model — the pipeline and
# all 9 downstream subagents — resolves its section from that file instead of
# the global default, so a formal test run can pin its own model combination
# without editing the shared config. Falls back to src/configs/llm.yaml.
LLM_CONFIG_ENV: str = "EP_AGENT_LLM_CONFIG"

_DEFAULT_LLM_CONFIG: Path = (
    Path(__file__).resolve().parent.parent / "configs" / "llm.yaml"
)


def resolve_llm_config_path() -> Path:
    """Active LLM config file: `$EP_AGENT_LLM_CONFIG` if set, else the global default."""
    override = os.environ.get(LLM_CONFIG_ENV)
    if override:
        p = Path(override)
        if not p.is_file():
            raise FileNotFoundError(
                f"{LLM_CONFIG_ENV}={override} does not point to a file"
            )
        return p
    return _DEFAULT_LLM_CONFIG


def load_llm_section(node_name: str | None) -> dict[str, Any]:
    """Resolve `node_name` to the right section of the active llm config.

    Three layouts supported:
      - Role routes: schema_version/services/models/roles, strict named role
        lookup, explicit default_role for unnamed callers.
      - Flat: top-level `provider`/`model_name`/... — single shared LLM (legacy).
      - Nested: top-level keys are section names (`default`, `intake`, ...);
        unknown `node_name` falls back to `default`.

    Config file = `resolve_llm_config_path()` (per-case override or global
    default). Public so non-langchain callers (e.g. src/agent/legacy/pipeline.py, which
    uses a raw OpenAI client) read the same config home.
    """
    raw = OmegaConf.load(resolve_llm_config_path())
    data = OmegaConf.to_container(raw, resolve=True)
    assert isinstance(data, dict), "llm.yaml must be a mapping"

    if "schema_version" in data:
        from src.agent.model_routes import ModelRoutes

        return ModelRoutes.model_validate(data).chat_config(node_name)

    if "provider" in data and "model_name" in data:
        return data  # flat / legacy layout

    if node_name and node_name in data:
        return data[node_name]
    if "default" in data:
        return data["default"]
    raise RuntimeError(
        f"llm.yaml has no 'default' section and no section matching node_name={node_name!r}"
    )


def create_llm(
    config: LLMConfig | None = None,
    node_name: str | None = None,
) -> BaseChatModel:
    """Create a LangChain chat model from llm.yaml.

    Args:
        config: Optional override. If None, reads `src/configs/llm.yaml`.
        node_name: Section name to pick when llm.yaml uses the multi-section
            layout. `intake_node` passes "intake"; the 9 downstream subagents
            pass nothing (defaults to the `default` section).

    Returns:
        A BaseChatModel routed to the configured provider.
    """
    if config is None:
        config = LLMConfig.model_validate(load_llm_section(node_name))

    model_id = f"{config.provider}:{config.model_name}"
    kwargs: dict[str, Any] = {
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
    }
    if config.base_url:
        kwargs["base_url"] = config.base_url
    if config.api_key:
        kwargs["api_key"] = config.api_key
    if config.extra_body:
        kwargs["extra_body"] = config.extra_body
    return init_chat_model(model_id, **kwargs)
