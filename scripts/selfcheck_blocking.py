"""Самопроверка обработки блокировок krisha.kz: BlockedError, повтор с паузой,
откат остальных поисков цикла, накопление подряд-циклов и разовый алерт.

Запуск: python scripts/selfcheck_blocking.py
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import krisha, main as beholder_main
from app.config import Search
from app.state import State

CARD = ('<div class="a-card ddl_product" data-id="{id}">'
        '<div class="a-card__title">T</div>'
        '<div class="a-card__price">1</div>'
        '<div class="a-card__subtitle">A</div>'
        '<div class="a-card__footer"><span class="user-owner"></span></div>'
        '</div>')


def page(total, *ids):
    cards = "".join(CARD.format(id=i) for i in ids)
    total_marker = f'"nbTotal":"{total}"' if total is not None else ""
    return f"<html>{total_marker}<body>{cards}</body></html>"


class FakeResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(str(self.status_code))


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, timeout=None):
        resp = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return resp


# 1) fetch_page: 429/403 -> BlockedError; прочие статусы — как раньше
for status in (429, 403):
    try:
        krisha.fetch_page(FakeSession([FakeResponse(status, "captcha")]), "http://x")
        raise AssertionError(f"ожидался BlockedError для {status}")
    except krisha.BlockedError:
        pass
assert krisha.fetch_page(FakeSession([FakeResponse(200, "ok")]), "http://x") == "ok"
print("fetch_page: 429/403 -> BlockedError, 200 -> ok")

# 2) fetch_all: нет карточек и нет nbTotal -> BlockedError (капча/анти-бот)
try:
    krisha.fetch_all(FakeSession([FakeResponse(200, "<html>captcha, no markers</html>")]),
                     "/x/", (), "b", 10, 0)
    raise AssertionError("ожидался BlockedError")
except krisha.BlockedError:
    pass
print("fetch_all: пустая страница без nbTotal -> BlockedError")

# 3) fetch_all: nbTotal>0, но карточки не распарсились -> ParseError (не BlockedError — смена вёрстки)
try:
    krisha.fetch_all(FakeSession([FakeResponse(200, page(5))]), "/x/", (), "b", 10, 0)
    raise AssertionError("ожидался ParseError")
except krisha.BlockedError:
    raise AssertionError("это должен быть ParseError, не BlockedError")
except krisha.ParseError:
    pass
print("fetch_all: nbTotal>0 без карточек -> ParseError (не BlockedError)")

# 4) fetch_all: nbTotal=0, карточек нет -> [] без исключений (реально пустой поиск)
assert krisha.fetch_all(FakeSession([FakeResponse(200, page(0))]), "/x/", (), "b", 10, 0) == []
print("fetch_all: nbTotal=0 -> genuine empty result, no exception")

# 5) fetch_all_with_retry: сначала блок, потом успех — без реального ожидания
sleeps = []
krisha.time.sleep = lambda s: sleeps.append(s)
session = FakeSession([FakeResponse(429, "blocked"), FakeResponse(200, page(1, "42"))])
listings = krisha.fetch_all_with_retry(session, "/x/", (), "b", 10, 0)
assert len(listings) == 1 and listings[0].id == "42"
assert sleeps == [20], sleeps
print("fetch_all_with_retry: block then success -> delivers data after one retry, sleeps:", sleeps)

# 6) fetch_all_with_retry: блок всегда -> BlockedError после исчерпания попыток
sleeps.clear()
session = FakeSession([FakeResponse(429, "blocked")])
try:
    krisha.fetch_all_with_retry(session, "/x/", (), "b", 10, 0)
    raise AssertionError("ожидался BlockedError")
except krisha.BlockedError:
    pass
assert sleeps == [20, 60], sleeps
print("fetch_all_with_retry: always blocked -> gives up after retries, sleeps:", sleeps)


# --- app.main: run_cycle откатывается на блоке, _track_blocking копит стрик и шлёт алерт один раз ---

def search(name, chat_id):
    return Search(name=name, url="https://krisha.kz/map/x/", enabled=True, chat_id=chat_id,
                  list_path="/x/", filters=(), lat=0.0, lon=0.0, zoom=14)


def fake_state():
    st = State(Path("unused"), {})
    st.save = lambda: None
    return st


def fake_bot():
    bot = SimpleNamespace(sent=[])
    bot.send_text = lambda chat_id, text: bot.sent.append((chat_id, text))
    bot.send_listing = lambda chat_id, listing, search_name: bot.sent.append(
        ("listing", chat_id, listing.id))
    return bot


cfg = SimpleNamespace(dry_run=False, viewport_px=(1000, 800), max_pages=10, page_delay_s=0,
                     admin_chat_id=None)

calls = []


def fetch_ok(session, list_path, filters, bounds, max_pages, page_delay_s):
    calls.append("ok")
    return [krisha.Listing(id="ok-1", title="t", price="p", address="a", seller="s")]


def fetch_blocked(session, list_path, filters, bounds, max_pages, page_delay_s):
    calls.append("blocked")
    raise krisha.BlockedError("blocked")


state = fake_state()
bot = fake_bot()
searches = [search("First", 1), search("Second", 2), search("Third", 3)]

# Первый поиск успешен, второй заблокирован — третий вообще не должен запрашиваться (откат цикла)
krisha.fetch_all_with_retry = lambda *a, **kw: (
    fetch_blocked(*a, **kw) if calls.count("ok") + calls.count("blocked") == 1 else fetch_ok(*a, **kw))
calls.clear()
beholder_main.run_cycle(cfg, state, bot, None, searches)
assert calls == ["ok", "blocked"], calls
assert state.blocked_streak == 1, state.blocked_streak
assert not state.blocked_notified
print("run_cycle: search #2 blocked -> search #3 skipped this cycle (back off), streak=1")

# Ещё BLOCKED_AFTER циклов подряд полностью заблокированы -> один раунд алерта
# (на каждого из 3 затронутых получателей — ровно по одному сообщению)
krisha.fetch_all_with_retry = lambda *a, **kw: fetch_blocked(*a, **kw)
for _ in range(beholder_main.BLOCKED_AFTER):
    beholder_main.run_cycle(cfg, state, bot, None, searches)
assert state.blocked_streak >= beholder_main.BLOCKED_AFTER
alerts = [s for s in bot.sent if isinstance(s[1], str) and "блокир" in s[1]]
assert len(alerts) == 3 and {t for t, _ in alerts} == {1, 2, 3}, alerts
print(f"run_cycle: {beholder_main.BLOCKED_AFTER}+ подряд заблокированных циклов -> "
      f"по одному алерту каждому из {len(alerts)} затронутых получателей")

# ещё один заблокированный цикл — повторного алерта быть не должно (уже уведомили)
beholder_main.run_cycle(cfg, state, bot, None, searches)
alerts_after = [s for s in bot.sent if isinstance(s[1], str) and "блокир" in s[1]]
assert len(alerts_after) == len(alerts), alerts_after
print("run_cycle: блокировка продолжается -> повторного алерта нет")

# сервис восстановился -> стрик и флаг сбрасываются
krisha.fetch_all_with_retry = fetch_ok
beholder_main.run_cycle(cfg, state, bot, None, searches)
assert state.blocked_streak == 0 and not state.blocked_notified
print("run_cycle: блокировка снята -> streak и notified сброшены")

print("OK: все проверки блокировки/повторов прошли")
