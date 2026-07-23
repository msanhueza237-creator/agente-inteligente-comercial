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


def is_chilean_mobile(raw: str | None) -> bool:
    """Return True when the value is a normalized Chilean mobile number."""
    phone = normalize_phone(raw)
    return bool(phone and phone.startswith("+569"))


def normalize_whatsapp_number(raw: str | None) -> str | None:
    """Return a WhatsApp-ready Chilean mobile number, or None for landlines."""
    phone = normalize_phone(raw)
    return phone if phone and phone.startswith("+569") else None
