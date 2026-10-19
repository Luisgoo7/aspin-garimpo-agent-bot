import asyncio

import httpx

from .config import settings

APPROVE = "a"
DISCARD = "d"


class TelegramError(RuntimeError):
    """Erro sanitizado (nunca inclui o token, que faz parte da URL da API)."""


async def _call(method: str, payload: dict, timeout: float = 20, require_result: bool = True):
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        raise TelegramError("Telegram não configurado")
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(3):
            try:
                response = await client.post(url, json=payload)
            except httpx.HTTPError:
                if attempt == 2:
                    raise TelegramError("Telegram: falha de conexão") from None
                await asyncio.sleep(2 ** attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                try:
                    delay = min(60, max(1, int(response.json().get("parameters", {}).get("retry_after", 2 ** attempt))))
                except (ValueError, TypeError, AttributeError):
                    delay = 2 ** attempt
                if attempt < 2:
                    await asyncio.sleep(delay)
                    continue
            try:
                data = response.json()
            except ValueError:
                raise TelegramError(f"Telegram HTTP {response.status_code}: resposta inválida") from None
            if response.status_code == 409:
                raise TelegramError("Telegram HTTP 409: o bot tem webhook ativo ou outro leitor de updates; "
                                    "desative o webhook/worker antigo para os botões funcionarem")
            if response.status_code >= 400 or data.get("ok") is not True:
                description = str(data.get("description") or "")[:120]
                raise TelegramError(f"Telegram HTTP {response.status_code}: {description or 'envio não confirmado'}")
            if require_result and data.get("result") in (None, False):
                raise TelegramError("Telegram: entrega não confirmada pela API")
            return data.get("result")
    raise TelegramError("Telegram: limite de tentativas atingido")


def decision_keyboard(opp_id) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Aprovar", "callback_data": f"{APPROVE}:{opp_id}"},
        {"text": "❌ Descartar", "callback_data": f"{DISCARD}:{opp_id}"},
    ]]}


def decided_keyboard(label: str) -> dict:
    return {"inline_keyboard": [[{"text": label[:60], "callback_data": "noop"}]]}


async def send_message(text: str, reply_markup: dict | None = None, html: bool = False) -> dict:
    payload = {"chat_id": settings.telegram_chat_id, "text": text[:4096], "disable_web_page_preview": True}
    if html:
        payload["parse_mode"] = "HTML"
    if reply_markup:
        payload["reply_markup"] = reply_markup
    result = await _call("sendMessage", payload)
    if not isinstance(result, dict) or not result.get("message_id"):
        raise TelegramError("Telegram: entrega não confirmada pela API")
    await asyncio.sleep(1.1)  # limite de ~1 msg/s por chat
    return {"message_id": result["message_id"], "chat_id": str(result.get("chat", {}).get("id", settings.telegram_chat_id))}


async def send_alert(text: str) -> None:
    await send_message(text)


async def get_updates(offset: int | None, poll_seconds: int = 0) -> list[dict]:
    """poll_seconds > 0 = long polling: a chamada fica aberta e volta assim que houver clique."""
    payload = {"timeout": poll_seconds, "allowed_updates": ["callback_query"], "limit": 100}
    if offset is not None:
        payload["offset"] = offset
    result = await _call("getUpdates", payload, timeout=poll_seconds + 20, require_result=False)
    return result if isinstance(result, list) else []


async def answer_callback(callback_id: str, text: str) -> None:
    try:
        await _call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})
    except TelegramError:
        pass  # clique antigo (>15 min) não aceita resposta; a mensagem é atualizada mesmo assim


async def set_buttons(chat_id, message_id, reply_markup: dict) -> None:
    try:
        await _call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup})
    except TelegramError as exc:
        if "not modified" not in str(exc):
            raise
