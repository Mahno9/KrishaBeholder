"""Самопроверка группировки уведомлений по chat_id в app.main (без сети, без pytest).

Запуск: python scripts/selfcheck_grouping.py
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Search
from app.krisha import Listing
from app.main import FLOOD_LIMIT, _dispatch_group, _group_fresh
from app.state import State


def search(name: str, chat_id: int) -> Search:
    return Search(name=name, url="https://krisha.kz/map/x/", enabled=True, chat_id=chat_id,
                  list_path="/x/", filters=(), lat=0.0, lon=0.0, zoom=14)


def listing(ad_id: str) -> Listing:
    return Listing(id=ad_id, title=f"Listing {ad_id}", price="1", address="a", seller="s")


def fake_state() -> State:
    state = State(Path("unused"), {})
    state.save = lambda: None  # no disk I/O in this script
    return state


def fake_bot():
    bot = SimpleNamespace(sent=[])
    bot.send_text = lambda chat_id, text: bot.sent.append(("text", chat_id, text))
    bot.send_listing = lambda chat_id, listing, search_name: bot.sent.append(
        ("listing", chat_id, listing.id, search_name))
    return bot


cfg = SimpleNamespace(dry_run=False)

# 1) две записи одного chat_id, разные ad_id -> одна группа с обоими
by_chat = _group_fresh([(search("A", 1), [listing("1")]), (search("B", 1), [listing("2")])])
assert list(by_chat.keys()) == [1]
assert set(by_chat[1].keys()) == {"1", "2"}

# 2) разные chat_id -> отдельные группы, ничего не теряется
by_chat = _group_fresh([(search("A", 1), [listing("1")]), (search("B", 2), [listing("2")])])
assert set(by_chat.keys()) == {1, 2}
assert set(by_chat[1].keys()) == {"1"}
assert set(by_chat[2].keys()) == {"2"}

# 3) один ad_id у двух поисков одного chat_id -> дедуп, имя первого поиска
by_chat = _group_fresh([(search("A", 1), [listing("1")]), (search("B", 1), [listing("1")])])
assert by_chat[1]["1"] == (listing("1"), "A")

# 4) один ad_id у двух поисков разных chat_id -> НЕ дедуплицируется
by_chat = _group_fresh([(search("A", 1), [listing("1")]), (search("B", 2), [listing("1")])])
assert "1" in by_chat[1] and "1" in by_chat[2]

# 5) FLOOD_LIMIT+1 у одного chat_id -> одна сводка, ноль send_listing;
#    другой, обычный chat_id в том же прогоне -> поштучно
state = fake_state()
bot = fake_bot()
flood_group = {str(i): (listing(str(i)), "A") for i in range(FLOOD_LIMIT + 1)}
normal_group = {"n1": (listing("n1"), "B")}
_dispatch_group(cfg, state, bot, 1, flood_group)
_dispatch_group(cfg, state, bot, 2, normal_group)
flood_sent = [s for s in bot.sent if s[1] == 1]
normal_sent = [s for s in bot.sent if s[1] == 2]
assert len(flood_sent) == 1 and flood_sent[0][0] == "text"
assert len(normal_sent) == 1 and normal_sent[0][0] == "listing"

# 6) chat_id на паузе -> ноль отправок, но объявление помечено просмотренным
state = fake_state()
state.paused.add(3)
bot = fake_bot()
_dispatch_group(cfg, state, bot, 3, {"p1": (listing("p1"), "A")})
assert bot.sent == []
assert "p1" in state.seen

print("OK: все проверки группировки прошли")
