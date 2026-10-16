import re

KEYWORDS = [
    "ofício requisitório", "oficio requisitorio", "requisição de pagamento",
    "requisicao de pagamento", "expedição de rpv", "expedicao de rpv",
    "precatório expedido", "precatorio expedido", "rpv expedida",
]

def keyword_score(text: str) -> float:
    normalized = text.casefold()
    hits = sum(1 for term in KEYWORDS if term in normalized)
    return min(0.95, 0.25 + hits * 0.15)

def extract_amount(text: str) -> float | None:
    match = re.search(r"R\$\s*([\d.]+,\d{2})", text)
    if not match:
        return None
    return float(match.group(1).replace('.', '').replace(',', '.'))
