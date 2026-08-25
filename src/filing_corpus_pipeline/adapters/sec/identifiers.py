"""Validation and normalization for SEC identifiers."""


def normalize_cik(value: str) -> str:
    """Return the ten-digit CIK form required by SEC endpoints."""
    stripped = value.strip()
    if not stripped.isascii() or not stripped.isdigit() or len(stripped) > 10:
        raise ValueError(f"invalid SEC CIK: {value!r}")
    return stripped.zfill(10)
