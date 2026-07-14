import tldextract

_extract = tldextract.TLDExtract(suffix_list_urls=())


def normalize_website(raw: str | None) -> str | None:
    """Return the root domain (e.g. `climaactiva.cl`) of a URL/domain string,
    or None if it can't be parsed into a registrable domain."""
    if not raw or not raw.strip():
        return None

    candidate = raw.strip()
    if "//" not in candidate:
        candidate = f"//{candidate}"

    extracted = _extract(candidate)
    if not extracted.domain or not extracted.suffix:
        return None

    return f"{extracted.domain}.{extracted.suffix}".lower()
