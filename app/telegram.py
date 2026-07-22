"""Минимальный клиент Telegram Bot API: discovery chat_id и отправка уведомлений."""

import html
import logging
import time

import requests

from .krisha import Listing

log = logging.getLogger(__name__)


class TelegramBot:
    def __init__(self, token: str):
        self._base = f"https://api.telegram.org/bot{token}"
        self._session = requests.Session()

    def api(self, method: str, **params) -> dict:
        response = self._session.post(f"{self._base}/{method}", json=params, timeout=35)
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description')}")
        return data["result"]

    def discover_chat_id(self, username: str, *, once: bool = False) -> int | None:
        """Ищет chat_id по сообщению от @username через getUpdates.

        В обычном режиме ждёт бесконечно. При once=True (разовый прогон на cron)
        опрашивает не дольше ~25 с и возвращает None, если сообщения ещё нет —
        следующий запуск попробует снова.
        """
        self.api("deleteWebhook")
        log.info("Ожидаю сообщение (например /start) от @%s боту...", username)
        offset = 0
        last_hint = 0.0
        deadline = time.monotonic() + 25 if once else None
        while True:
            try:
                updates = self.api("getUpdates", timeout=20, offset=offset)
            except requests.RequestException:
                log.exception("getUpdates не удался%s", "" if once else ", повтор через 10 с")
                if once:
                    return None
                time.sleep(10)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                sender = (message.get("from") or {}).get("username") or ""
                if sender.lower() == username:
                    chat_id = message["chat"]["id"]
                    log.info("Найден chat_id=%s для @%s", chat_id, username)
                    self.send_text(chat_id, "Подключено. Слежу за новыми квартирами на krisha.kz — "
                                            "уведомления будут приходить сюда.")
                    return chat_id
            if deadline is not None and time.monotonic() >= deadline:
                return None
            if time.monotonic() - last_hint > 60:
                log.info("Всё ещё жду, чтобы @%s написал(а) боту /start...", username)
                last_hint = time.monotonic()

    def send_text(self, chat_id: int, text: str) -> None:
        self.api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")

    def send_listing(self, chat_id: int, listing: Listing, search_name: str) -> None:
        parts = [f"🏠 <b>{html.escape(listing.title)}</b>"]
        details = " · ".join(p for p in (listing.price, listing.address, listing.seller) if p)
        if details:
            parts.append(html.escape(details))
        parts.append(f"🔍 {html.escape(search_name)}")
        parts.append(listing.url)
        self.send_text(chat_id, "\n".join(parts))
