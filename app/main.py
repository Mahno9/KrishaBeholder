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


def _group_fresh(pairs: list[tuple[config.Search, list[krisha.Listing]]]
                  ) -> dict[int, dict[str, tuple[krisha.Listing, str]]]:
    """Группирует свежие объявления по chat_id получателя; дедуп по ad_id — только внутри одного chat_id."""
    by_chat: dict[int, dict[str, tuple[krisha.Listing, str]]] = {}
    for search, fresh in pairs:
        group = by_chat.setdefault(search.chat_id, {})
        for listing in fresh:
            group.setdefault(listing.id, (listing, search.name))
    return by_chat


def _dispatch_group(cfg: config.Config, state: State, bot: TelegramBot | None,
                     chat_id: int, group: dict[str, tuple[krisha.Listing, str]]) -> None:
    if cfg.dry_run or bot is None:
        for listing, search_name in group.values():
            log.info("[dry-run] Новое: %s | %s | %s | %s", listing.title, listing.price,
                     search_name, listing.url)
            state.mark_seen(listing.id, search_name, notified=False)
        state.save()
        return
    if chat_id in state.paused:
        for listing, search_name in group.values():
            state.mark_seen(listing.id, search_name, notified=False)
        state.save()
        log.info("chat_id=%s на паузе — %d новых помечены без уведомления", chat_id, len(group))
        return
    if len(group) > FLOOD_LIMIT:
        log.warning("chat_id=%s: сразу %d новых — похоже на аномалию, шлю сводку", chat_id, len(group))
        bot.send_text(chat_id,
                      f"⚠️ За цикл появилось сразу {len(group)} новых объявлений — "
                      "возможен сбой парсинга или изменение области. Проверьте поиск на сайте.")
        for listing, search_name in group.values():
            state.mark_seen(listing.id, search_name, notified=False)
        state.save()
        return
    for listing, search_name in group.values():
        bot.send_listing(chat_id, listing, search_name)
        state.mark_seen(listing.id, search_name, notified=True)
        state.save()
        log.info("Отправлено: %s (%s) -> chat_id=%s", listing.url, search_name, chat_id)
        time.sleep(1)


def run_cycle(cfg: config.Config, state: State, bot: TelegramBot | None,
              session: requests.Session, searches: list[config.Search]) -> None:
    pairs: list[tuple[config.Search, list[krisha.Listing]]] = []

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
        elif fresh:
            pairs.append((search, fresh))
        time.sleep(cfg.page_delay_s)

    new_by_chat = _group_fresh(pairs)
    if not new_by_chat:
        log.info("Новых объявлений нет")
    else:
        for chat_id, group in new_by_chat.items():
            _dispatch_group(cfg, state, bot, chat_id, group)

    state.last_success = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state.save()


def poll_commands(bot: TelegramBot, state: State) -> None:
    """Короткий (не блокирующий) опрос /start и /stop; прочие апдейты пропускаются."""
    try:
        bot.api("deleteWebhook")
        updates = bot.get_updates(state.update_offset)
    except Exception:
        log.exception("Не удалось опросить команды — пропускаю до следующего цикла")
        return
    for update in updates:
        state.update_offset = update["update_id"] + 1
        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip().lower()
        if chat_id is None:
            continue
        try:
            if text == "/start":
                state.paused.discard(chat_id)
                bot.send_text(chat_id, f"Готово. Ваш chat_id: {chat_id}\n"
                                        "Если рассылка была на паузе — она возобновлена.")
            elif text == "/stop":
                state.paused.add(chat_id)
                bot.send_text(chat_id, "Рассылка приостановлена. Пришлите /start, чтобы возобновить.")
        except Exception:
            log.exception("Не удалось обработать команду от chat_id=%s", chat_id)
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

    searches = config.load_searches(cfg.searches_path, fallback=[], inline_json=cfg.searches_json)
    if not searches:
        log.warning("Нет валидных поисков (SEARCHES_JSON / %s) — жду, пока их добавят "
                    "(команды /start и /stop продолжают работать)", cfg.searches_path)

    bot: TelegramBot | None = None
    if cfg.bot_token:
        bot = TelegramBot(cfg.bot_token)
    elif not cfg.dry_run:
        log.error("TELEGRAM_BOT_TOKEN не задан — выхожу")
        return 1

    failures = 0
    degraded_notified = False
    while True:
        searches = config.load_searches(cfg.searches_path, fallback=searches, inline_json=cfg.searches_json)
        if bot is not None and not cfg.dry_run:
            try:
                poll_commands(bot, state)
            except Exception:
                log.exception("Опрос команд завершился с ошибкой")
        try:
            run_cycle(cfg, state, bot, session, searches)
            failures = 0
            degraded_notified = False
        except Exception:
            failures += 1
            log.exception("Цикл завершился с ошибкой (%d подряд)", failures)
            if failures >= DEGRADED_AFTER and not degraded_notified and bot:
                targets = {s.chat_id for s in searches if s.enabled} - state.paused
                if cfg.admin_chat_id is not None:
                    targets.add(cfg.admin_chat_id)
                for target in targets:
                    try:
                        bot.send_text(target,
                                      f"⚠️ Сервис не может обновить данные уже {failures} циклов подряд.")
                    except Exception:
                        log.exception("Не удалось отправить сообщение о деградации в chat_id=%s", target)
                degraded_notified = True
        if cfg.run_once:
            return 0
        delay = cfg.poll_interval_s + random.uniform(0, 120)
        log.info("Следующая проверка через %.0f мин", delay / 60)
        time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
