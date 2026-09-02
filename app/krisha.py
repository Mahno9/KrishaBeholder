"""Загрузка и парсинг списочных страниц krisha.kz."""

import logging
import math
import re
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE = "https://krisha.kz"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
PER_PAGE = 20
_NB_TOTAL_RE = re.compile(r'nbTotal":"?(\d[\d\s]*)')


class ParseError(Exception):
    pass


class BlockedError(ParseError):
    """krisha.kz, похоже, блокирует наши запросы (капча/анти-бот/лимит) — не ошибка вёрстки."""


BLOCK_RETRY_DELAYS_S = (20, 60)  # паузы перед повторными попытками при признаках блокировки


@dataclass(frozen=True)
class Listing:
    id: str
    title: str
    price: str
    address: str
    seller: str

    @property
    def url(self) -> str:
        return f"{BASE}/a/show/{self.id}"


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ru"})
    return session


def build_list_url(list_path: str, filters: tuple[tuple[str, str], ...],
                   bounds: str, page: int) -> str:
    params = [("bounds", bounds), *filters]
    if page > 1:
        params.append(("page", str(page)))
    return f"{BASE}{list_path}?{urlencode(params)}"


def fetch_page(session: requests.Session, url: str) -> str:
    response = session.get(url, timeout=15)
    if response.status_code in (403, 429):
        raise BlockedError(f"HTTP {response.status_code} на {url} — похоже на блокировку/анти-бот")
    response.raise_for_status()
    return response.text


def parse_total(html: str) -> int | None:
    match = _NB_TOTAL_RE.search(html)
    return int(match.group(1).replace(" ", "")) if match else None


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)) if node else ""


def parse_listings(html: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    listings = []
    for card in soup.select("div.a-card.ddl_product[data-id]"):
        ad_id = card["data-id"]
        seller = "unknown"
        footer = card.select_one(".a-card__footer")
        if footer:
            for cls, label in (("user-owner", "хозяин"),
                               ("user-specialist", "специалист"),
                               ("user-company", "компания")):
                if cls in footer.get("class", []):
                    seller = label
                    break
        listings.append(Listing(
            id=ad_id,
            title=_text(card.select_one(".a-card__title")),
            price=_text(card.select_one(".a-card__price")),
            address=_text(card.select_one(".a-card__subtitle")),
            seller=seller,
        ))
    return listings


def fetch_all(session: requests.Session, list_path: str,
              filters: tuple[tuple[str, str], ...], bounds: str,
              max_pages: int, page_delay_s: float) -> list[Listing]:
    """Обходит все страницы поиска.

    Бросает BlockedError при признаках анти-бота (капча/лимит — нет карточек и
    не нашёлся nbTotal), ParseError — при подозрении на смену вёрстки.
    """
    url = build_list_url(list_path, filters, bounds, page=1)
    html = fetch_page(session, url)
    total = parse_total(html)
    listings = parse_listings(html)
    if not listings:
        if total is None:
            raise BlockedError(f"нет карточек и не нашёлся nbTotal — похоже на блокировку/капчу: {url}")
        if total > 0:
            raise ParseError(f"nbTotal={total}, но карточки не распарсились: {url}")

    pages = min(math.ceil((total or 0) / PER_PAGE), max_pages) if total else 1
    if total and total > max_pages * PER_PAGE:
        log.warning("Найдено %d объявлений, обходятся только первые %d страниц — "
                    "стоит сузить фильтры", total, max_pages)

    seen_ids = {listing.id for listing in listings}
    for page in range(2, pages + 1):
        time.sleep(page_delay_s)
        page_listings = parse_listings(fetch_page(
            session, build_list_url(list_path, filters, bounds, page)))
        for listing in page_listings:
            if listing.id not in seen_ids:
                seen_ids.add(listing.id)
                listings.append(listing)
    log.info("Обход %s: nbTotal=%s, страниц=%d, уникальных карточек=%d",
             list_path, total, pages, len(listings))
    return listings


def fetch_all_with_retry(session: requests.Session, list_path: str,
                         filters: tuple[tuple[str, str], ...], bounds: str,
                         max_pages: int, page_delay_s: float) -> list[Listing]:
    """Как fetch_all, но при признаках блокировки ждёт и пробует снова, прежде чем сдаться.

    Так разовый анти-бот/капча-ответ не срывает доставку — данные просто приходят
    с небольшой задержкой вместо ошибки.
    """
    for attempt, delay in enumerate(BLOCK_RETRY_DELAYS_S):
        try:
            return fetch_all(session, list_path, filters, bounds, max_pages, page_delay_s)
        except BlockedError:
            log.warning("Похоже на блокировку krisha.kz — жду %d с и пробую снова (%d/%d)",
                        delay, attempt + 1, len(BLOCK_RETRY_DELAYS_S))
            time.sleep(delay)
    return fetch_all(session, list_path, filters, bounds, max_pages, page_delay_s)
