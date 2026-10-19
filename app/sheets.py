"""Envio das oportunidades aprovadas para a planilha Google (Apps Script Web App)."""
import httpx

from .config import settings


class SheetsError(RuntimeError):
    """Erro sanitizado (sem URL do Web App nem token)."""


def configured() -> bool:
    return bool(settings.sheets_webhook_url.strip() and settings.sheets_token.strip())


async def append_row(row: dict) -> bool:
    """Grava uma linha. True = gravada agora ou já existia (o script deduplica pelo ID interno)."""
    if not configured():
        raise SheetsError("planilha não configurada (SHEETS_WEBHOOK_URL/SHEETS_TOKEN)")
    async with httpx.AsyncClient(timeout=40, follow_redirects=True) as client:
        try:
            response = await client.post(settings.sheets_webhook_url.strip(),
                                          json={"token": settings.sheets_token.strip(), "row": row})
        except httpx.HTTPError as exc:
            raise SheetsError(f"planilha: rede ({type(exc).__name__})") from None
    if response.status_code >= 400:
        raise SheetsError(f"planilha: HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        # Web App sem acesso "Qualquer pessoa" devolve a página de login do Google.
        raise SheetsError("planilha: resposta não é JSON (confira se o Web App está com acesso 'Qualquer pessoa')") from None
    if data.get("ok") is not True:
        raise SheetsError(f"planilha: recusada ({str(data.get('error') or 'sem detalhe')[:60]})")
    return True
