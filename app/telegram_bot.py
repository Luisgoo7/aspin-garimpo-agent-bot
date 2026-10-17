import asyncio
import httpx
from .config import settings


async def send_alert(text: str) -> None:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise RuntimeError("Telegram não configurado")
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=20) as client:
        for attempt in range(3):
            try:
                response = await client.post(url, json={"chat_id": settings.telegram_chat_id, "text": text[:4096], "disable_web_page_preview": True})
            except httpx.HTTPError:
                if attempt == 2:
                    raise RuntimeError("Telegram: falha de conexão") from None
                await asyncio.sleep(2 ** attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                try:
                    data = response.json()
                    delay = min(60, max(1, int(data.get("parameters", {}).get("retry_after", 2 ** attempt))))
                except (ValueError, TypeError):
                    delay = 2 ** attempt
                if attempt < 2:
                    await asyncio.sleep(delay)
                    continue
            if response.status_code >= 400:
                raise RuntimeError(f"Telegram HTTP {response.status_code}: envio não confirmado")
            try:
                payload = response.json()
            except ValueError:
                raise RuntimeError("Telegram: resposta inválida") from None
            if payload.get("ok") is not True or not payload.get("result", {}).get("message_id"):
                raise RuntimeError("Telegram: entrega não confirmada pela API")
            await asyncio.sleep(1.1)
            return
    raise RuntimeError("Telegram: limite de tentativas atingido")
