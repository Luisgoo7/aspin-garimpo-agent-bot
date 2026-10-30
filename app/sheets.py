"""Envio das oportunidades aprovadas para a planilha Google (Apps Script Web App)."""
import httpx

from .config import settings


class SheetsError(RuntimeError):
    """Erro sanitizado (sem URL do Web App nem token)."""


def configured() -> bool:
    return bool(settings.sheets_webhook_url.strip() and settings.sheets_token.strip())


LAYOUT_VERSION = 5  # v5: colunas do CRM (Etapa, Responsável, Próxima ação...) e abas Listas/Funil
CRM_EXPORT_VERSION = 7  # v7: ação crm_export (leitura das negociações para os alertas do bot)


async def script_version() -> int:
    """Versão do script publicado (GET). O script antigo não informa versão (= 1)."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        try:
            response = await client.get(settings.sheets_webhook_url.strip())
            return int(response.json().get("version") or 1)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return 0


async def format_sheet() -> bool:
    """Pede ao script da planilha para reorganizar colunas e aplicar a formatação.

    Só envia se o script publicado já for da versão nova: o antigo trataria o pedido
    como uma linha e gravaria uma linha em branco.
    """
    if not configured():
        raise SheetsError("planilha não configurada (SHEETS_WEBHOOK_URL/SHEETS_TOKEN)")
    if await script_version() < LAYOUT_VERSION:
        raise SheetsError("planilha: script desatualizado (cole a versão nova e publique uma nova versão)")
    return await _post({"token": settings.sheets_token.strip(), "action": "format"}, expect="formatted")


async def append_row(row: dict) -> bool:
    """Grava uma linha. True = gravada agora ou já existia (o script deduplica pelo ID interno)."""
    if not configured():
        raise SheetsError("planilha não configurada (SHEETS_WEBHOOK_URL/SHEETS_TOKEN)")
    return await _post({"token": settings.sheets_token.strip(), "row": row})


async def crm_export() -> dict | None:
    """Lê as negociações do CRM ({"rows", "stages", "appUrl"}).

    None = script publicado ainda não tem a ação (versão < 7) ou planilha não configurada.
    Nesse caso nada é enviado: o script antigo gravaria o pedido como uma linha em branco.
    """
    if not configured() or await script_version() < CRM_EXPORT_VERSION:
        return None
    data = await _request({"token": settings.sheets_token.strip(), "action": "crm_export"})
    if not isinstance(data.get("rows"), list):
        raise SheetsError("planilha: crm_export sem a lista de linhas")
    data["rows"] = [r for r in data["rows"] if isinstance(r, dict)]
    return data


async def _post(payload: dict, expect: str | None = None) -> bool:
    data = await _request(payload)
    if expect and not data.get(expect):
        raise SheetsError("planilha: script desatualizado (cole a versão nova e publique uma nova versão)")
    return True


async def _request(payload: dict) -> dict:
    """POST ao Web App; devolve o JSON já validado (ok=true) ou levanta SheetsError sanitizado."""
    if not configured():
        raise SheetsError("planilha não configurada (SHEETS_WEBHOOK_URL/SHEETS_TOKEN)")
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        try:
            response = await client.post(settings.sheets_webhook_url.strip(), json=payload)
        except httpx.HTTPError as exc:
            raise SheetsError(f"planilha: rede ({type(exc).__name__})") from None
    if response.status_code >= 400:
        raise SheetsError(f"planilha: HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError:
        # Web App sem acesso "Qualquer pessoa" devolve a página de login do Google.
        raise SheetsError("planilha: resposta não é JSON (confira se o Web App está com acesso 'Qualquer pessoa')") from None
    if not isinstance(data, dict) or data.get("ok") is not True:
        detail = data.get("error") if isinstance(data, dict) else None
        raise SheetsError(f"planilha: recusada ({str(detail or 'sem detalhe')[:60]})")
    return data
