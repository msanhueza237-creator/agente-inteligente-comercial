import re

_CLEAN_RE = re.compile(r"[^0-9kK]")


def _check_digit(number: int) -> str:
    total = 0
    multiplier = 2
    for digit in reversed(str(number)):
        total += int(digit) * multiplier
        multiplier = multiplier + 1 if multiplier < 7 else 2
    remainder = 11 - (total % 11)
    if remainder == 11:
        return "0"
    if remainder == 10:
        return "K"
    return str(remainder)


def normalize_rut(raw: str | None) -> str | None:
    """Validate a Chilean RUT (modulo-11 check digit) and return it in
    canonical `XXXXXXXX-D` form, or None if missing/invalid."""
    if not raw or not raw.strip():
        return None

    cleaned = _CLEAN_RE.sub("", raw.upper())
    if len(cleaned) < 2:
        return None

    number_part, given_check = cleaned[:-1], cleaned[-1]
    if not number_part.isdigit():
        return None

    number = int(number_part)
    if number <= 0:
        return None

    if _check_digit(number) != given_check:
        return None

    return f"{number}-{given_check}"
