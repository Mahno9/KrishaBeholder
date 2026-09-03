"""Конфигурация из переменных окружения и searches.json."""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Search:
    name: str
    url: str
    enabled: bool
    chat_id: int                        # получатель уведомлений этого поиска
    list_path: str                      # /arenda/kvartiry/ — путь карты без /map
    filters: tuple[tuple[str, str], ...]  # das[...] и прочие параметры как есть
    lat: float
    lon: float
    zoom: int


@dataclass
class Config:
    bot_token: str
    admin_chat_id: int | None
    poll_interval_s: int
    max_pages: int
    page_delay_s: float
    state_path: Path
    searches_path: Path
    searches_json: str
    viewport_px: tuple[int, int]
    run_once: bool
    dry_run: bool
    gist_id: str
    gist_token: str
    gist_filename: str
    gist_searches_filename: str


def _env(name: str, default: str) -> str:
    """Значение переменной окружения; пустая строка трактуется как «не задано».

    GitHub Actions передаёт неопределённые vars как "", поэтому нельзя полагаться
    на второй аргумент os.environ.get — нужен явный откат на default.
    """
    value = os.environ.get(name, "").strip()
    return value or default


def load_config(argv: list[str] | None = None) -> Config:
    argv = argv or []
    w, _, h = _env("VIEWPORT_PX", "1000x800").partition("x")
    admin_chat_id_raw = _env("ADMIN_CHAT_ID", "")
    return Config(
        bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        admin_chat_id=int(admin_chat_id_raw) if admin_chat_id_raw else None,
        poll_interval_s=int(_env("POLL_INTERVAL_S", "1800")),
        max_pages=int(_env("MAX_PAGES", "10")),
        page_delay_s=float(_env("PAGE_DELAY_S", "2.5")),
        state_path=Path(_env("STATE_PATH", "/data/state.json")),
        searches_path=Path(_env("SEARCHES_PATH", "/data/searches.json")),
        searches_json=os.environ.get("SEARCHES_JSON", "").strip(),
        viewport_px=(int(w), int(h)),
        run_once=os.environ.get("RUN_ONCE", "") == "1" or "--once" in argv,
        dry_run=os.environ.get("DRY_RUN", "") == "1" or "--dry-run" in argv,
        gist_id=os.environ.get("GIST_ID", "").strip(),
        gist_token=os.environ.get("GIST_TOKEN", "").strip(),
        gist_filename=_env("GIST_FILENAME", "krisha-state.json"),
        gist_searches_filename=_env("GIST_SEARCHES_FILENAME", "krisha-searches.json"),
    )


def parse_map_url(name: str, url: str, enabled: bool, chat_id: int) -> Search:
    parts = urlsplit(url)
    if not parts.path.startswith("/map/"):
        raise ValueError(f"поиск '{name}': ожидается ссылка на карту (/map/...), получено {parts.path}")
    list_path = parts.path.removeprefix("/map")

    lat = lon = None
    zoom = 14
    filters: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "lat":
            lat = float(value)
        elif key == "lon":
            lon = float(value)
        elif key == "zoom":
            zoom = int(value)
        elif key == "bounds":
            continue  # пересчитываем сами из lat/lon/zoom
        else:
            filters.append((key, value))
    if lat is None or lon is None:
        raise ValueError(f"поиск '{name}': в ссылке нет lat/lon")
    return Search(name=name, url=url, enabled=enabled, chat_id=chat_id, list_path=list_path,
                  filters=tuple(filters), lat=lat, lon=lon, zoom=zoom)


def load_searches(path: Path, fallback: list[Search], inline_json: str = "") -> list[Search]:
    """Читает searches: из inline_json (переменная SEARCHES_JSON), если задана, иначе из файла path.

    SEARCHES_JSON позволяет менять получателей и поиски прямо в GitHub Actions
    (Settings → Secrets and variables → Actions → Variables) без коммита —
    формат тот же {"searches": [...]}, что и в searches.json.
    При ошибке (в т.ч. невалидный JSON) — предыдущая валидная версия.
    """
    source = "SEARCHES_JSON" if inline_json else str(path)
    try:
        raw_text = inline_json if inline_json else path.read_text(encoding="utf-8")
        raw = json.loads(raw_text)
        searches = []
        names = set()
        for item in raw["searches"]:
            name = item["name"]
            if name in names:
                raise ValueError(f"дублирующееся имя поиска '{name}'")
            names.add(name)
            chat_id = int(item["chat_id"])
            searches.append(parse_map_url(name, item["url"], item.get("enabled", True), chat_id))
        if not searches:
            raise ValueError("список searches пуст")
        return searches
    except Exception:
        log.exception("Не удалось прочитать %s — использую предыдущую версию (%d поисков)",
                      source, len(fallback))
        return fallback
