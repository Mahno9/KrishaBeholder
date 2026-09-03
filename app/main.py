"""KrishaBeholder: мониторинг новых объявлений krisha.kz с уведомлениями в Telegram."""

import logging
import random
import sys
import time
from datetime import datetime, timezone

import requests

from . import config, geo, krisha, subscriptions
from .state import State
from .store import GistStore
from .telegram import TelegramBot

log = logging.getLogger("beholder")

FLOOD_LIMIT = 25          # больше новых за цикл — одно сводное сообщение вместо спама
DEGRADED_AFTER = 5        # подряд неудачных циклов до сообщения о деградации
BLOCKED_AFTER = 3         # подряд циклов с признаками блокировки krisha.kz до алерта


def _alert_targets(cfg: config.Config, state: State, searches: list[config.Search]) -> set[int]:
    targets = {s.chat_id for s in searches if s.enabled} - state.paused
    if cfg.admin_chat_id is not None:
        targets.add(cfg.admin_chat_id)
    return targets


def _track_blocking(cfg: config.Config, state: State, bot: TelegramBot | None,
                     searches: list[config.Search], blocked: int) -> None:
    """Копит подряд-циклы с блокировкой и один раз шлёт алерт после BLOCKED_AFTER."""
    if blocked > 0:
        state.blocked_streak += 1
    else:
        if state.blocked_streak:
            log.info("krisha.kz снова отвечает нормально (блокировка длилась %d цикл(ов))",
                      state.blocked_streak)
        state.blocked_streak = 0
        state.blocked_notified = False

    if (state.blocked_streak >= BLOCKED_AFTER and not state.blocked_notified
            and bot is not None and not cfg.dry_run):
        text = (f"⚠️ krisha.kz, похоже, блокирует наши запросы (антиспам/лимит) уже "
                f"{state.blocked_streak} циклов подряд. Как только блокировка снимется, "
                "рассылка продолжится сама — делать ничего не нужно.")
        for target in _alert_targets(cfg, state, searches):
            try:
                bot.send_text(target, text)
            except Exception:
                log.exception("Не удалось отправить сообщение о блокировке в chat_id=%s", target)
        state.blocked_notified = True


def _baseline_key(search: config.Search) -> str:
    """chat_id+имя, а не голое имя: у разных пользователей могут совпадать имена
    (боты-подписки нумеруют «Поиск 1».."Поиск 3» в рамках одного chat_id)."""
    return f"{search.chat_id}:{search.name}"


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
    blocked = 0

    for search in searches:
        if not search.enabled:
            continue
        bounds = geo.bounds_param(search.lat, search.lon, search.zoom, *cfg.viewport_px)
        try:
            listings = krisha.fetch_all_with_retry(session, search.list_path, search.filters,
                                                    bounds, cfg.max_pages, cfg.page_delay_s)
        except krisha.BlockedError:
            blocked += 1
            log.warning("Поиск '%s': похоже на блокировку krisha.kz даже после повторов — "
                        "прекращаю запросы до следующего цикла", search.name)
            break
        except Exception:
            log.exception("Поиск '%s' не удался, пропускаю до следующего цикла", search.name)
            continue

        fresh = [listing for listing in listings if listing.id not in state.seen]
        baseline_key = _baseline_key(search)
        if not state.baselines.get(baseline_key):
            for listing in fresh:
                state.mark_seen(listing.id, search.name, notified=False)
            state.baselines[baseline_key] = True
            state.save()
            log.info("Тихая база поиска '%s' (chat_id=%s): запомнено %d объявлений",
                      search.name, search.chat_id, len(fresh))
        elif fresh:
            pairs.append((search, fresh))
        time.sleep(cfg.page_delay_s)

    _track_blocking(cfg, state, bot, searches, blocked)

    new_by_chat = _group_fresh(pairs)
    if not new_by_chat:
        log.info("Новых объявлений нет")
    else:
        for chat_id, group in new_by_chat.items():
            _dispatch_group(cfg, state, bot, chat_id, group)

    state.last_success = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state.save()


def _notify(bot: TelegramBot, chat_id: int, text: str) -> None:
    """Best-effort уведомление: сбой отправки не должен откатывать уже случившуюся мутацию данных."""
    try:
        bot.send_text(chat_id, text)
    except Exception:
        log.exception("Не удалось отправить сообщение в chat_id=%s", chat_id)


def _handle_subscribe(bot: TelegramBot, cfg: config.Config, searches_store: GistStore | None,
                       searches: list[config.Search], pending: list[dict],
                       chat_id: int, url: str) -> bool:
    if searches_store is None:
        _notify(bot, chat_id, "Самостоятельная подписка сейчас недоступна — обратитесь к администратору.")
        return False
    if not url:
        _notify(bot, chat_id, "Пришлите ссылку на карту krisha.kz: /subscribe https://krisha.kz/map/...")
        return False
    try:
        entry = subscriptions.add_pending(searches, pending, chat_id, url)
    except ValueError as exc:
        _notify(bot, chat_id, f"Не получилось: {exc}")
        return False
    # Мутация уже случилась (entry в pending) — дальше только best-effort уведомления.
    slot = subscriptions.slot_of(entry["name"])
    _notify(bot, chat_id, f"Заявка «{entry['name']}» принята, ждите подтверждения администратора.")
    if cfg.admin_chat_id is not None:
        _notify(bot, cfg.admin_chat_id,
                f"Новая заявка от {chat_id}: {entry['url']}\n"
                f"Одобрить: /approve {chat_id} {slot}\n"
                f"Отклонить: /reject {chat_id} {slot}")
    else:
        _notify(bot, chat_id, "Внимание: у бота не задан ADMIN_CHAT_ID — подтвердить заявку пока некому.")
    return True


def _handle_subs(bot: TelegramBot, searches: list[config.Search], pending: list[dict],
                  chat_id: int) -> None:
    items = subscriptions.list_own(searches, pending, chat_id)
    if not items:
        _notify(bot, chat_id, "У вас нет подписок. Добавить: /subscribe https://krisha.kz/map/...")
        return
    lines = [f"{slot}. {url}" + (" (ожидает одобрения)" if is_pending else "")
             for slot, url, is_pending in items]
    _notify(bot, chat_id, "Ваши подписки:\n" + "\n".join(lines) + "\n\nОтписаться: /unsubscribe <номер>")


def _handle_unsubscribe(bot: TelegramBot, searches: list[config.Search], pending: list[dict],
                        chat_id: int, arg: str) -> bool:
    try:
        slot = int(arg)
        url = subscriptions.unsubscribe(searches, pending, chat_id, slot)
    except ValueError:
        _notify(bot, chat_id, "Укажите номер подписки: /unsubscribe <номер> (список — /subs)")
        return False
    # Мутация уже случилась — уведомление дальше best-effort.
    _notify(bot, chat_id, f"Подписка №{slot} ({url}) удалена.")
    return True


def _handle_admin_decision(bot: TelegramBot, cfg: config.Config, searches: list[config.Search],
                           pending: list[dict], chat_id: int, is_approve: bool, arg: str) -> bool:
    if chat_id != cfg.admin_chat_id:
        _notify(bot, chat_id, "Эта команда доступна только администратору.")
        return False
    try:
        target_chat_id_s, slot_s = arg.split()
        target_chat_id, slot = int(target_chat_id_s), int(slot_s)
        if is_approve:
            search = subscriptions.approve(searches, pending, target_chat_id, slot)
        else:
            entry = subscriptions.reject(pending, target_chat_id, slot)
    except (ValueError, IndexError) as exc:
        _notify(bot, chat_id, f"Не получилось: {exc}\nФормат: /approve <chat_id> <номер>")
        return False
    # Мутация уже случилась (approve/reject применены) — уведомления дальше best-effort.
    if is_approve:
        _notify(bot, chat_id, f"Одобрено: {search.name} для {target_chat_id}.")
        _notify(bot, target_chat_id, f"Подписка №{slot} подтверждена и активна.")
    else:
        _notify(bot, chat_id, f"Отклонено: {entry['name']} для {target_chat_id}.")
        _notify(bot, target_chat_id, f"Заявка №{slot} отклонена администратором.")
    return True


def poll_commands(bot: TelegramBot, state: State, cfg: config.Config,
                   searches_store: GistStore | None,
                   searches: list[config.Search], pending: list[dict]
                   ) -> tuple[list[config.Search], list[dict]]:
    """Короткий (не блокирующий) опрос команд; прочие апдейты пропускаются."""
    try:
        bot.api("deleteWebhook")
        updates = bot.get_updates(state.update_offset)
    except Exception:
        log.exception("Не удалось опросить команды — пропускаю до следующего цикла")
        return searches, pending

    changed = False
    for update in updates:
        state.update_offset = update["update_id"] + 1
        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        if chat_id is None or not text:
            continue
        lower = text.lower()
        try:
            if lower == "/start":
                state.paused.discard(chat_id)
                bot.send_text(chat_id, f"Готово. Ваш chat_id: {chat_id}\n"
                                        "Если рассылка была на паузе — она возобновлена.")
            elif lower == "/stop":
                state.paused.add(chat_id)
                bot.send_text(chat_id, "Рассылка приостановлена. Пришлите /start, чтобы возобновить.")
            elif lower.startswith("/subscribe"):
                changed |= _handle_subscribe(bot, cfg, searches_store, searches, pending,
                                             chat_id, text[len("/subscribe"):].strip())
            elif lower == "/subs":
                _handle_subs(bot, searches, pending, chat_id)
            elif lower.startswith("/unsubscribe"):
                changed |= _handle_unsubscribe(bot, searches, pending, chat_id,
                                               text[len("/unsubscribe"):].strip())
            elif lower.startswith("/approve") or lower.startswith("/reject"):
                changed |= _handle_admin_decision(bot, cfg, searches, pending, chat_id,
                                                  lower.startswith("/approve"),
                                                  text.partition(" ")[2].strip())
        except Exception:
            log.exception("Не удалось обработать команду от chat_id=%s", chat_id)
    state.save()
    if changed and searches_store is not None:
        try:
            subscriptions.save(searches_store, searches, pending)
        except Exception:
            log.exception("Не удалось сохранить список подписок в гист")
    return searches, pending


def _load_searches(cfg: config.Config, searches_store: GistStore | None,
                    fallback: list[config.Search]) -> tuple[list[config.Search], list[dict]]:
    """Гист (если настроен и там уже что-то есть) в приоритете, иначе SEARCHES_JSON/файл."""
    file_env = config.load_searches(cfg.searches_path, fallback=fallback, inline_json=cfg.searches_json)
    return subscriptions.load(searches_store, fallback=file_env)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load_config(sys.argv[1:])
    store = None
    searches_store = None
    if cfg.gist_id and cfg.gist_token:
        store = GistStore(cfg.gist_id, cfg.gist_token, cfg.gist_filename)
        searches_store = GistStore(cfg.gist_id, cfg.gist_token, cfg.gist_searches_filename)
        log.info("Состояние хранится в gist %s (%s), подписки — в %s",
                  cfg.gist_id, cfg.gist_filename, cfg.gist_searches_filename)
    state = State.load(cfg.state_path, store)
    session = krisha.make_session()

    searches, pending = _load_searches(cfg, searches_store, [])
    if not searches:
        log.warning("Нет валидных поисков (гист/SEARCHES_JSON/%s) — жду, пока их добавят "
                    "(команды /start, /stop, /subscribe продолжают работать)", cfg.searches_path)

    bot: TelegramBot | None = None
    if cfg.bot_token:
        bot = TelegramBot(cfg.bot_token)
    elif not cfg.dry_run:
        log.error("TELEGRAM_BOT_TOKEN не задан — выхожу")
        return 1

    while True:
        searches, pending = _load_searches(cfg, searches_store, searches)
        if bot is not None and not cfg.dry_run:
            try:
                searches, pending = poll_commands(bot, state, cfg, searches_store, searches, pending)
            except Exception:
                log.exception("Опрос команд завершился с ошибкой")
        try:
            run_cycle(cfg, state, bot, session, searches)
            if state.failures or state.degraded_notified:
                state.failures = 0
                state.degraded_notified = False
                state.save()
        except Exception:
            state.failures += 1
            log.exception("Цикл завершился с ошибкой (%d подряд)", state.failures)
            if state.failures >= DEGRADED_AFTER and not state.degraded_notified and bot:
                for target in _alert_targets(cfg, state, searches):
                    try:
                        bot.send_text(target,
                                      f"⚠️ Сервис не может обновить данные уже {state.failures} циклов подряд.")
                    except Exception:
                        log.exception("Не удалось отправить сообщение о деградации в chat_id=%s", target)
                state.degraded_notified = True
            state.save()
        if cfg.run_once:
            return 0
        delay = cfg.poll_interval_s + random.uniform(0, 120)
        log.info("Следующая проверка через %.0f мин", delay / 60)
        time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
