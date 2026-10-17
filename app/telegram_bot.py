import asyncio
import httpx
from .config import settings

async def send_alert(text: str) -> None:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise RuntimeError("Telegram não configurado")
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=20) as client:
        for attempt in range(3):
            response = await client.post(url, json={"chat_id": settings.telegram_chat_id, "text": text, "disable_web_page_preview": True})
            if response.status_code == 429:
                retry_after = response.headers.get("retry-after", "2")
                try:
                    delay = min(60, int(retry_after) + 1)
                except ValueError:
                    delay = 3
                await asyncio.sleep(delay)
                continue
            response.raise_for_status()
            await asyncio.sleep(1.1)
            return
        raise RuntimeError("Telegram continuou limitando o envio após 3 tentativas")
