"""Хранение состояния в GitHub Gist — для эфемерных раннеров (GitHub Actions).

Локальный файл на раннере исчезает после каждого запуска, поэтому состояние
(chat_id, тихие базы, список виденных объявлений) держим в приватном gist.
"""

import logging

import requests

log = logging.getLogger(__name__)

API = "https://api.github.com"


class GistStore:
    def __init__(self, gist_id: str, token: str, filename: str):
        self.gist_id = gist_id
        self.filename = filename
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def read(self) -> str | None:
        """Содержимое файла состояния в gist или None, если его там ещё нет."""
        response = self._session.get(f"{API}/gists/{self.gist_id}", timeout=30)
        response.raise_for_status()
        file = response.json().get("files", {}).get(self.filename)
        if not file:
            return None
        if file.get("truncated"):  # gist вернул усечённый content — тянем полный
            raw = self._session.get(file["raw_url"], timeout=30)
            raw.raise_for_status()
            return raw.text
        return file.get("content")

    def write(self, content: str) -> None:
        response = self._session.patch(
            f"{API}/gists/{self.gist_id}",
            json={"files": {self.filename: {"content": content}}},
            timeout=30,
        )
        response.raise_for_status()
