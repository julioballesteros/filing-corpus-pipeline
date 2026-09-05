"""Environment configuration parsing shared by Lambda handlers."""

import os


class LambdaConfigurationError(RuntimeError):
    """Required Lambda environment configuration is missing or invalid."""


def required_environment(name: str) -> str:
    """Read one required, non-blank environment setting."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise LambdaConfigurationError(f"{name} must be configured")
    return value


def positive_environment_integer(name: str, *, maximum: int) -> int:
    """Read one positive integer bounded by a safe runtime maximum."""
    value = required_environment(name)
    try:
        parsed = int(value)
    except ValueError as error:
        raise LambdaConfigurationError(
            f"{name} must be a positive integer no greater than {maximum}"
        ) from error
    if parsed < 1 or parsed > maximum:
        raise LambdaConfigurationError(
            f"{name} must be a positive integer no greater than {maximum}"
        )
    return parsed
