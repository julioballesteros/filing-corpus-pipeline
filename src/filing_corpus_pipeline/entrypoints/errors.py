"""Shared runtime configuration errors for Lambda entrypoints."""


class LambdaConfigurationError(RuntimeError):
    """Raised when required Lambda environment configuration is invalid."""
