from app.normalization.phone import normalize_phone, normalize_whatsapp_number


def test_chilean_mobile_is_whatsapp_ready() -> None:
    assert normalize_phone("+56 9 4415 1740") == "+56944151740"
    assert normalize_whatsapp_number("+56 9 4415 1740") == "+56944151740"


def test_chilean_landline_is_not_whatsapp_ready() -> None:
    assert normalize_phone("+56 2 2345 6789") == "+56223456789"
    assert normalize_whatsapp_number("+56 2 2345 6789") is None
