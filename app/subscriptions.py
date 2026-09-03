"""Самообслуживание подписок пользователей через Telegram.

Поток: /subscribe -> заявка в pending -> администратор /approve -> активный
поиск. Хранится вторым файлом в том же гисте, что и state.json — тот же
GIST_TOKEN, новый секрет не нужен. Имя поиска пользователь не придумывает —
боты сами нумеруют «Поиск 1».."Поиск {MAX_PER_USER}" в пределах одного chat_id;
глобальной уникальности имён (как для searches.json/SEARCHES_JSON) здесь не
требуется — run_cycle различает поиски разных пользователей по (chat_id, имя).
"""

import json
import logging
import re

from . import config
from .store import GistStore

log = logging.getLogger(__name__)

MAX_PER_USER = 3
_SLOT_RE = re.compile(r"^Поиск (\d+)$")


def _name_for_slot(n: int) -> str:
    return f"Поиск {n}"


def slot_of(name: str) -> int | None:
    match = _SLOT_RE.match(name)
    return int(match.group(1)) if match else None


def _used_slots(searches: list[config.Search], pending: list[dict], chat_id: int) -> set[int]:
    slots = {slot_of(s.name) for s in searches if s.chat_id == chat_id}
    slots |= {slot_of(p["name"]) for p in pending if p["chat_id"] == chat_id}
    slots.discard(None)
    return slots


def _next_slot(searches: list[config.Search], pending: list[dict], chat_id: int) -> int | None:
    used = _used_slots(searches, pending, chat_id)
    for n in range(1, MAX_PER_USER + 1):
        if n not in used:
            return n
    return None


def _parse_blob(raw_text: str) -> tuple[list[config.Search], list[dict]]:
    raw = json.loads(raw_text)
    searches = [
        config.parse_map_url(item["name"], item["url"], item.get("enabled", True), int(item["chat_id"]))
        for item in raw.get("searches", [])
    ]
    pending = [
        {"name": item["name"], "url": item["url"], "chat_id": int(item["chat_id"])}
        for item in raw.get("pending", [])
    ]
    return searches, pending


def load(store: GistStore | None, fallback: list[config.Search]
         ) -> tuple[list[config.Search], list[dict]]:
    """searches/pending из гиста; при отсутствии файла или ошибке — fallback и пустой pending."""
    if store is None:
        return fallback, []
    try:
        raw_text = store.read()
    except Exception:
        log.exception("Не удалось прочитать список подписок из гиста — использую предыдущую версию")
        return fallback, []
    if raw_text is None:
        return fallback, []
    try:
        return _parse_blob(raw_text)
    except Exception:
        log.exception("Список подписок в гисте повреждён — использую предыдущую версию")
        return fallback, []


def save(store: GistStore, searches: list[config.Search], pending: list[dict]) -> None:
    payload = {
        "searches": [{"name": s.name, "url": s.url, "enabled": s.enabled, "chat_id": s.chat_id}
                     for s in searches],
        "pending": pending,
    }
    store.write(json.dumps(payload, ensure_ascii=False, indent=1))


def add_pending(searches: list[config.Search], pending: list[dict], chat_id: int, url: str) -> dict:
    """Валидирует ссылку и лимит MAX_PER_USER, добавляет заявку. Бросает ValueError с текстом для юзера."""
    slot = _next_slot(searches, pending, chat_id)
    if slot is None:
        raise ValueError(f"у вас уже {MAX_PER_USER} подписки — больше нельзя, "
                          "отпишитесь от одной через /subs")
    name = _name_for_slot(slot)
    config.parse_map_url(name, url, True, chat_id)  # только валидация ссылки
    entry = {"name": name, "url": url, "chat_id": chat_id}
    pending.append(entry)
    return entry


def approve(searches: list[config.Search], pending: list[dict], chat_id: int, slot: int) -> config.Search:
    name = _name_for_slot(slot)
    for i, entry in enumerate(pending):
        if entry["chat_id"] == chat_id and entry["name"] == name:
            search = config.parse_map_url(entry["name"], entry["url"], True, entry["chat_id"])
            searches.append(search)
            pending.pop(i)
            return search
    raise ValueError(f"заявка chat_id={chat_id} №{slot} не найдена")


def reject(pending: list[dict], chat_id: int, slot: int) -> dict:
    name = _name_for_slot(slot)
    for i, entry in enumerate(pending):
        if entry["chat_id"] == chat_id and entry["name"] == name:
            return pending.pop(i)
    raise ValueError(f"заявка chat_id={chat_id} №{slot} не найдена")


def unsubscribe(searches: list[config.Search], pending: list[dict], chat_id: int, slot: int) -> str:
    """Убирает свою (pending или активную) подписку №slot, возвращает её url. Бросает ValueError."""
    name = _name_for_slot(slot)
    for i, entry in enumerate(pending):
        if entry["chat_id"] == chat_id and entry["name"] == name:
            return pending.pop(i)["url"]
    for i, search in enumerate(searches):
        if search.chat_id == chat_id and search.name == name:
            return searches.pop(i).url
    raise ValueError(f"подписки №{slot} нет")


def list_own(searches: list[config.Search], pending: list[dict], chat_id: int
             ) -> list[tuple[int, str, bool]]:
    """(slot, url, is_pending) для подписок этого chat_id по возрастанию slot."""
    items = [(slot_of(s.name), s.url, False) for s in searches if s.chat_id == chat_id]
    items += [(slot_of(p["name"]), p["url"], True) for p in pending if p["chat_id"] == chat_id]
    items = [i for i in items if i[0] is not None]
    items.sort(key=lambda t: t[0])
    return items
