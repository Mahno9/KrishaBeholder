"""Минимальный клиент Telegram Bot API: опрос команд и отправка уведомлений."""

import html
import logging

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

    def get_updates(self, offset: int, timeout: int = 0) -> list[dict]:
        """Забирает накопившиеся апдейты без долгого long-polling (timeout=0 — сразу)."""
        return self.api("getUpdates", offset=offset, timeout=timeout)

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
