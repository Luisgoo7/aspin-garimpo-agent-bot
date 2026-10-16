import httpx
from .config import settings

async def send_alert(text: str) -> None:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise RuntimeError("Telegram não configurado")
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(url, json={"chat_id": settings.telegram_chat_id, "text": text, "disable_web_page_preview": True})
        response.raise_for_status()
