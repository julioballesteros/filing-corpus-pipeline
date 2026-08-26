"""Shared Pydantic primitives for immutable pipeline contracts."""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, ValidationError


def _require_nonempty(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be empty")
    return value


def _normalize_sha256(value: str) -> str:
    if len(value) != 64:
        raise ValueError("must be a 64-character hexadecimal digest")
    try:
        bytes.fromhex(value)
    except ValueError as error:
        raise ValueError("must be a 64-character hexadecimal digest") from error
    return value.lower()


def _require_lowercase_sha256(value: str) -> str:
    normalized = _normalize_sha256(value)
    if value != normalized:
        raise ValueError("must be a lowercase SHA-256 digest")
    return value


NonEmptyString = Annotated[str, AfterValidator(_require_nonempty)]
Sha256Digest = Annotated[str, AfterValidator(_normalize_sha256)]
LowercaseSha256Digest = Annotated[str, AfterValidator(_require_lowercase_sha256)]


class PipelineModel(BaseModel):
    """Strict-shape, immutable base for data crossing pipeline boundaries."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
    )


def validation_error_message(error: ValidationError) -> str:
    """Render bounded, stable validation details for an external boundary."""
    details: list[str] = []
    for item in error.errors(
        include_url=False, include_context=False, include_input=False
    ):
        location = ".".join(str(segment) for segment in item["loc"])
        prefix = f"{location}: " if location else ""
        details.append(f"{prefix}{item['msg']}")
    return "; ".join(details)
