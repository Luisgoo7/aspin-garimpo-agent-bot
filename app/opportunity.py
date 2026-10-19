import html
import math
import re
import unicodedata

KEYWORDS = [
    "oficio requisitorio", "requisicao de pagamento", "expedicao de rpv",
    "precatorio expedido", "rpv expedida", "rpv expedido", "rpv",
    "oficio de pagamento", "ordem de pagamento", "requisicao expedida",
    "precatorio", "requisicao", "r.p.v.", "precatorio alimentar",
]


def clean_text(text: str) -> str:
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", str(text or ""), flags=re.I | re.S)
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", text)).split())


def normalized_text(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", clean_text(text).casefold()) if not unicodedata.combining(c))


def keyword_score(text: str) -> float:
    text = normalized_text(text)
    hits = sum(bool(re.search(r"\b" + re.escape(term) + r"\b", text)) for term in KEYWORDS)
    return min(0.95, 0.25 + hits * 0.15)


def parse_amount(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            value = value.strip().replace("R$", "").replace(" ", "")
            if "," in value:
                value = value.replace(".", "").replace(",", ".")
            elif re.fullmatch(r"\d{1,3}(\.\d{3})+", value):
                value = value.replace(".", "")
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (ValueError, TypeError):
        return None


def extract_amount(text: str) -> float | None:
    match = re.search(r"R\$\s*([\d.]+,\d{2})", clean_text(text))
    return parse_amount(match.group(1)) if match else None


def canonical_alias(alias: str) -> str:
    alias = str(alias or "djen").strip().casefold()
    return alias if alias.startswith("api_publica_") or alias == "djen" else "api_publica_" + alias


def canonical_cnj(value: str) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if len(digits) == 20 else str(value or "").strip()


_TEXT_AMOUNT_RE = re.compile(r"(?i)(?:r\$|\bvalor\b[^\d]{0,24})\s*(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+,\d{1,2})")


def max_amount_in_text(text: str) -> float | None:
    """Maior valor monetário citado (formato brasileiro), usado só para triagem."""
    values = [parse_amount(m.group(1)) for m in _TEXT_AMOUNT_RE.finditer(clean_text(text))]
    values = [v for v in values if v is not None]
    return max(values) if values else None


def focus_excerpt(text: str, max_chars: int = 6000) -> str:
    """Trecho em torno do maior valor citado, para caber no limite de tokens da IA gratuita."""
    text = clean_text(text)
    if len(text) <= max_chars:
        return text
    best = None
    for match in _TEXT_AMOUNT_RE.finditer(text):
        value = parse_amount(match.group(1))
        if value is not None and (best is None or value > best[0]):
            best = (value, match.start())
    center = best[1] if best else 0
    start = max(0, min(center - max_chars // 2, len(text) - max_chars))
    return text[start:start + max_chars]
