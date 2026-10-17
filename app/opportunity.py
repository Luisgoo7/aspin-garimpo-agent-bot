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
