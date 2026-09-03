"""Самопроверка самообслуживания подписок: /subscribe -> approve/reject, лимит
3 на пользователя, автонумерация слотов, /subs, /unsubscribe, и что сбой
отправки уведомления не откатывает уже случившуюся мутацию данных.

Запуск: python scripts/selfcheck_subscriptions.py
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import krisha, subscriptions
from app import main as beholder_main
from app.config import Search
from app.state import State

URL = "https://krisha.kz/map/arenda/kvartiry/?zoom=14&lat=1&lon=2"


def fake_bot(fail_for: frozenset = frozenset()):
    bot = SimpleNamespace(sent=[])

    def send_text(chat_id, text):
        if chat_id in fail_for:
            raise RuntimeError(f"Telegram недоступен для {chat_id}")
        bot.sent.append((chat_id, text))
    bot.send_text = send_text
    return bot


class FakeStore:
    def __init__(self):
        self.written = None

    def write(self, content):
        self.written = content


# --- чистые функции subscriptions.py ---

searches: list[Search] = []
pending: list[dict] = []

e1 = subscriptions.add_pending(searches, pending, 111, URL)
assert e1["name"] == "Поиск 1" and e1["chat_id"] == 111
e2 = subscriptions.add_pending(searches, pending, 111, URL)
assert e2["name"] == "Поиск 2"
e3 = subscriptions.add_pending(searches, pending, 111, URL)
assert e3["name"] == "Поиск 3"
try:
    subscriptions.add_pending(searches, pending, 111, URL)
    raise AssertionError("ожидался ValueError на 4-ю подписку")
except ValueError as exc:
    assert "3" in str(exc)
print("add_pending: лимит 3 на пользователя соблюдается, имена — Поиск 1..3")

other = subscriptions.add_pending(searches, pending, 222, URL)
assert other["name"] == "Поиск 1" and other["chat_id"] == 222
print("add_pending: имена не глобальные — у другого chat_id тоже 'Поиск 1', без конфликта")

before = len(pending)
try:
    subscriptions.add_pending(searches, pending, 333, "https://example.com/not-krisha")
    raise AssertionError("ожидался ValueError на плохую ссылку")
except ValueError:
    pass
assert len(pending) == before
print("add_pending: невалидная ссылка отклоняется, pending не меняется")

search = subscriptions.approve(searches, pending, 111, 1)
assert search.name == "Поиск 1" and search.chat_id == 111 and search.enabled
assert not any(p["chat_id"] == 111 and p["name"] == "Поиск 1" for p in pending)
assert any(s.chat_id == 111 and s.name == "Поиск 1" for s in searches)
print("approve: заявка стала активным поиском")

try:
    subscriptions.approve(searches, pending, 111, 1)
    raise AssertionError("повторный approve уже одобренной/несуществующей заявки должен падать")
except ValueError:
    pass
print("approve: повторный approve -> ValueError")

rejected = subscriptions.reject(pending, 111, 2)
assert rejected["name"] == "Поиск 2"
assert not any(s.chat_id == 111 and s.name == "Поиск 2" for s in searches)
print("reject: заявка удалена, в активные не попала")

e_new = subscriptions.add_pending(searches, pending, 111, URL)
assert e_new["name"] == "Поиск 2"
print("add_pending: освободившийся слот переиспользуется (2, не 4)")

url1 = subscriptions.unsubscribe(searches, pending, 111, 1)
assert url1 == URL
assert not any(s.chat_id == 111 and s.name == "Поиск 1" for s in searches)
subscriptions.unsubscribe(searches, pending, 222, 1)  # своя "Поиск 1", не чужая
assert not any(s.chat_id == 222 and s.name == "Поиск 1" for s in searches)
print("unsubscribe: убирает только собственную подписку под указанным номером")

try:
    subscriptions.unsubscribe(searches, pending, 111, 99)
    raise AssertionError("ожидался ValueError для несуществующего номера")
except ValueError:
    pass
print("unsubscribe: несуществующий номер -> ValueError")

items = subscriptions.list_own(searches, pending, 111)
assert [i[0] for i in items] == sorted(i[0] for i in items)
print("list_own: отсортировано по номеру -", items)


# --- app.main: командные обработчики поверх Telegram-слоя ---

cfg = SimpleNamespace(admin_chat_id=999)
searches2: list[Search] = []
pending2: list[dict] = []
store = FakeStore()

bot = fake_bot()
changed = beholder_main._handle_subscribe(bot, cfg, store, searches2, pending2, 111, URL)
assert changed and len(pending2) == 1
assert any("Заявка" in t for c, t in bot.sent if c == 111)
assert any("Новая заявка" in t for c, t in bot.sent if c == 999)
print("_handle_subscribe: заявка создана, уведомлены пользователь и админ")

# Ключевая проверка: сбой отправки подтверждения НЕ должен откатывать уже
# случившуюся мутацию (заявка в pending остаётся).
bot_failing = fake_bot(fail_for=frozenset({111}))
changed2 = beholder_main._handle_subscribe(bot_failing, cfg, store, searches2, pending2, 111, URL)
assert changed2 is True and len(pending2) == 2
print("_handle_subscribe: сбой отправки подтверждения не откатывает мутацию")

bot = fake_bot()
ok = beholder_main._handle_admin_decision(bot, cfg, searches2, pending2, 999, True, "111 1")
assert ok and any(s.chat_id == 111 and s.name == "Поиск 1" for s in searches2)
print("_handle_admin_decision: /approve админом активирует подписку")

bot = fake_bot()
ok = beholder_main._handle_admin_decision(bot, cfg, searches2, pending2, 111, True, "111 2")
assert not ok
assert any("администратору" in t for c, t in bot.sent if c == 111)
print("_handle_admin_decision: не-админ не может approve/reject")

bot_failing = fake_bot(fail_for=frozenset({111}))
ok = beholder_main._handle_admin_decision(bot_failing, cfg, searches2, pending2, 999, True, "111 2")
assert ok and any(s.chat_id == 111 and s.name == "Поиск 2" for s in searches2)
print("_handle_admin_decision: сбой уведомления не откатывает approve")

bot = fake_bot()
ok = beholder_main._handle_unsubscribe(bot, searches2, pending2, 111, "1")
assert ok and not any(s.chat_id == 111 and s.name == "Поиск 1" for s in searches2)
print("_handle_unsubscribe: убирает активную подписку")


# --- run_cycle: одинаковое имя у разных chat_id не путает тихую базу ---

def search_obj(name, chat_id):
    return Search(name=name, url=URL, enabled=True, chat_id=chat_id,
                 list_path="/x/", filters=(), lat=0.0, lon=0.0, zoom=14)


def fetch_returning(ad_id):
    def _fetch(session, list_path, filters, bounds, max_pages, page_delay_s):
        return [krisha.Listing(id=ad_id, title="t", price="p", address="a", seller="s")]
    return _fetch


state3 = State(Path("unused"), {})
state3.save = lambda: None
cfg3 = SimpleNamespace(dry_run=False, viewport_px=(1000, 800), max_pages=10, page_delay_s=0,
                       admin_chat_id=None)
bot3 = fake_bot()

krisha.fetch_all_with_retry = fetch_returning("ad-A")
beholder_main.run_cycle(cfg3, state3, bot3, None, [search_obj("Поиск 1", 111)])
assert state3.baselines == {"111:Поиск 1": True}, state3.baselines
assert "ad-A" in state3.seen

krisha.fetch_all_with_retry = fetch_returning("ad-B")
beholder_main.run_cycle(cfg3, state3, bot3, None, [search_obj("Поиск 1", 222)])
assert state3.baselines == {"111:Поиск 1": True, "222:Поиск 1": True}, state3.baselines
assert "ad-B" in state3.seen
assert bot3.sent == []
print("run_cycle: одинаковое имя 'Поиск 1' у разных chat_id не путает тихую базу")

print("OK: все проверки подписок прошли")
