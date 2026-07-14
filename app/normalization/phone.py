import phonenumbers


def normalize_phone(raw: str | None) -> str | None:
    """Return an E.164 Chilean phone number, or None if unparseable/invalid."""
    if not raw or not raw.strip():
        return None
    try:
        parsed = phonenumbers.parse(raw, "CL")
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
