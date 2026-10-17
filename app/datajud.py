from typing import Any
import httpx
from .config import settings

BASE_URL = "https://api-publica.datajud.cnj.jus.br"

class DataJudClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.datajud_api_key

    async def search(self, tribunal_alias: str, query: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("DATAJUD_API_KEY não configurada")
        url = f"{BASE_URL}/{tribunal_alias}/_search"
        headers = {"Authorization": f"APIKey {self.api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(url, headers=headers, json=query)
            response.raise_for_status()
            return response.json()
