from pathlib import Path

from omegaconf import OmegaConf
from pydantic import BaseModel, ConfigDict, Field


class EmbeddingConfig(BaseModel):
    model_name: str = Field(default="gemini-embedding-001")
    dimension: int = Field(default=3072)
    task_type: str = Field(default="RETRIEVAL_DOCUMENT")

    model_config = ConfigDict(
        from_attributes=True,
        validate_assignment=True,
        arbitrary_types_allowed=True,
        str_strip_whitespace=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
    )

    def model_post_init(self, __context):
        config = OmegaConf.load(Path(__file__).parent / "embedding.yaml")
        self.model_name = config.model_name
        self.dimension = config.dimension
        self.task_type = config.task_type


class LLMConfig(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        validate_assignment=True,
        arbitrary_types_allowed=True,
        str_strip_whitespace=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
        frozen=True,
    )

    provider: str = Field(..., description="The provider of the LLM model")
    base_url: str | None = Field(
        default=None, description="The base URL of the LLM model"
    )
    model_name: str = Field(..., description="The name of the LLM model to use")
    temperature: float = Field(
        ..., ge=0.0, description="The temperature of the LLM model"
    )
    max_tokens: int = Field(
        ..., ge=0, description="The maximum number of tokens to generate"
    )
    api_key: str | None = Field(
        default=None, description="The API key of the LLM model"
    )
    extra_body: dict | None = Field(
        default=None,
        description="Provider-specific extra body params merged into the request "
        "(e.g. DeepSeek `thinking={'type':'disabled'}` to suppress reasoning_content).",
    )
