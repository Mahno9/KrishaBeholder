"""KrishaBeholder: мониторинг новых объявлений krisha.kz с уведомлениями в Telegram."""

import logging
import random
import sys
import time
from datetime import datetime, timezone

import requests

from . import config, geo, krisha
from .state import State
from .store import GistStore
from .telegram import TelegramBot

log = logging.getLogger("beholder")

FLOOD_LIMIT = 25          # больше новых за цикл — одно сводное сообщение вместо спама
DEGRADED_AFTER = 5        # подряд неудачных циклов до сообщения о деградации


def run_cycle(cfg: config.Config, state: State, bot: TelegramBot | None,
              session: requests.Session, searches: list[config.Search]) -> None:
    new_by_id: dict[str, tuple[krisha.Listing, str]] = {}

    for search in searches:
        if not search.enabled:
            continue
        bounds = geo.bounds_param(search.lat, search.lon, search.zoom, *cfg.viewport_px)
        try:
            listings = krisha.fetch_all(session, search.list_path, search.filters,
                                        bounds, cfg.max_pages, cfg.page_delay_s)
        except Exception:
            log.exception("Поиск '%s' не удался, пропускаю до следующего цикла", search.name)
            continue

        fresh = [listing for listing in listings if listing.id not in state.seen]
        if not state.baselines.get(search.name):
            for listing in fresh:
                state.mark_seen(listing.id, search.name, notified=False)
            state.baselines[search.name] = True
            state.save()
            log.info("Тихая база поиска '%s': запомнено %d объявлений", search.name, len(fresh))
        else:
            for listing in fresh:
                new_by_id.setdefault(listing.id, (listing, search.name))
        time.sleep(cfg.page_delay_s)

    if not new_by_id:
        log.info("Новых объявлений нет")
    elif cfg.dry_run or bot is None or state.chat_id is None:
        for listing, search_name in new_by_id.values():
            log.info("[dry-run] Новое: %s | %s | %s | %s", listing.title, listing.price,
                     search_name, listing.url)
            state.mark_seen(listing.id, search_name, notified=False)
        state.save()
    elif len(new_by_id) > FLOOD_LIMIT:
        log.warning("Найдено сразу %d новых — похоже на аномалию, шлю сводку", len(new_by_id))
        bot.send_text(state.chat_id,
                      f"⚠️ За цикл появилось сразу {len(new_by_id)} новых объявлений — "
                      "возможен сбой парсинга или изменение области. Проверьте поиск на сайте.")
        for listing, search_name in new_by_id.values():
            state.mark_seen(listing.id, search_name, notified=False)
        state.save()
    else:
        for listing, search_name in new_by_id.values():
            bot.send_listing(state.chat_id, listing, search_name)
            state.mark_seen(listing.id, search_name, notified=True)
            state.save()
            log.info("Отправлено: %s (%s)", listing.url, search_name)
            time.sleep(1)

    state.last_success = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state.save()


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load_config(sys.argv[1:])
    store = None
    if cfg.gist_id and cfg.gist_token:
        store = GistStore(cfg.gist_id, cfg.gist_token, cfg.gist_filename)
        log.info("Состояние хранится в gist %s (%s)", cfg.gist_id, cfg.gist_filename)
    state = State.load(cfg.state_path, store)
    session = krisha.make_session()

    searches = config.load_searches(cfg.searches_path, fallback=[])
    if not searches:
        log.error("Нет валидных поисков в %s — выхожу", cfg.searches_path)
        return 1

    bot: TelegramBot | None = None
    if cfg.bot_token:
        bot = TelegramBot(cfg.bot_token)
        if cfg.chat_id:
            state.chat_id = cfg.chat_id
        if state.chat_id is None and not cfg.dry_run:
            chat_id = bot.discover_chat_id(cfg.tg_username, once=cfg.run_once)
            if chat_id is None:
                log.warning("chat_id ещё не определён. Отправьте боту /start с аккаунта @%s — "
                            "следующий запуск его подхватит.", cfg.tg_username)
                return 0
            state.chat_id = chat_id
            state.save()
    elif not cfg.dry_run:
        log.error("TELEGRAM_BOT_TOKEN не задан — выхожу")
        return 1

    failures = 0
    degraded_notified = False
    while True:
        searches = config.load_searches(cfg.searches_path, fallback=searches)
        try:
            run_cycle(cfg, state, bot, session, searches)
            failures = 0
            degraded_notified = False
        except Exception:
            failures += 1
            log.exception("Цикл завершился с ошибкой (%d подряд)", failures)
            if (failures >= DEGRADED_AFTER and not degraded_notified
                    and bot and state.chat_id):
                try:
                    bot.send_text(state.chat_id,
                                  f"⚠️ Сервис не может обновить данные уже {failures} циклов подряд.")
                    degraded_notified = True
                except Exception:
                    log.exception("Не удалось отправить сообщение о деградации")
        if cfg.run_once:
            return 0
        delay = cfg.poll_interval_s + random.uniform(0, 120)
        log.info("Следующая проверка через %.0f мин", delay / 60)
        time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
