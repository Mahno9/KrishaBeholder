"""Персистентное состояние сервиса в JSON-файле (атомарная запись)."""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .store import GistStore

log = logging.getLogger(__name__)


class State:
    def __init__(self, path: Path, data: dict, store: GistStore | None = None):
        self.path = path
        self.store = store
        self.baselines: dict[str, bool] = data.get("baselines", {})
        self.seen: dict[str, dict] = data.get("seen", {})
        self.last_success: str | None = data.get("last_success")
        self.paused: set[int] = set(data.get("paused", []))
        self.update_offset: int = data.get("update_offset", 0)
        self.failures: int = data.get("failures", 0)
        self.degraded_notified: bool = data.get("degraded_notified", False)
        self.blocked_streak: int = data.get("blocked_streak", 0)
        self.blocked_notified: bool = data.get("blocked_notified", False)

    @classmethod
    def load(cls, path: Path, store: GistStore | None = None) -> "State":
        if store is not None:
            try:
                raw = store.read()
            except Exception:
                log.exception("Не удалось прочитать состояние из gist — пробую локальный файл")
            else:
                if raw is None:
                    log.info("В gist ещё нет состояния — начинаю с чистого листа")
                    return cls(path, {}, store)
                try:
                    return cls(path, json.loads(raw), store)
                except Exception:
                    log.exception("Состояние в gist повреждено — начинаю заново")
                    return cls(path, {}, store)
        try:
            if path.exists():
                return cls(path, json.loads(path.read_text(encoding="utf-8")), store)
        except Exception:
            backup = path.with_suffix(".json.bak")
            log.exception("Файл состояния повреждён, переименовываю в %s и начинаю заново", backup)
            os.replace(path, backup)
        return cls(path, {}, store)

    def save(self) -> None:
        payload = {
            "baselines": self.baselines,
            "seen": self.seen,
            "last_success": self.last_success,
            "paused": sorted(self.paused),
            "update_offset": self.update_offset,
            "failures": self.failures,
            "degraded_notified": self.degraded_notified,
            "blocked_streak": self.blocked_streak,
            "blocked_notified": self.blocked_notified,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=1)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)
        if self.store is not None:
            try:
                self.store.write(text)
            except Exception:
                log.exception("Не удалось сохранить состояние в gist (локальная копия записана)")

    def mark_seen(self, ad_id: str, search_name: str, notified: bool) -> None:
        self.seen[ad_id] = {
            "first_seen": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "notified": notified,
            "search": search_name,
        }
