"""Выгрузка карточек Zolla. Установка: python -m pip install lxml."""

import argparse
import csv
import json
import logging
import re
import time
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from lxml import html

SITEMAP = "https://zolla.com/sitemap.xml"
FIELDS = (
    "hierarchy", "name", "article", "current_price", "old_price",
    "out_of_stock", "product_url",
)


def fetch(url, delay):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; RetailCatalogParser/1.0)"})
    for attempt in range(3):
        time.sleep(delay)
        try:
            with urlopen(request, timeout=30) as response:
                return response.read()
        except HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise
            retry_after = error.headers.get("Retry-After", "")
            time.sleep(int(retry_after) if retry_after.isdecimal() else 2 ** (attempt + 1))
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 ** (attempt + 1))


def zolla_path(url):
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.netloc == "zolla.com" and not parts.query and not parts.fragment:
        return parts.path
    return ""


def is_product(url):
    return bool(re.fullmatch(r"/product/[^/]+/?", zolla_path(url)))


def discover_products(delay):
    pending = [SITEMAP]
    visited = set()
    while pending:
        sitemap = pending.pop()
        if sitemap in visited:
            continue
        visited.add(sitemap)
        root = ElementTree.fromstring(fetch(sitemap, delay))
        locations = [node.text.strip() for node in root.findall("./*/{*}loc") if node.text]
        kind = root.tag.rsplit("}", 1)[-1]
        if kind == "sitemapindex":
            pending.extend(reversed([url for url in locations if zolla_path(url)]))
        elif kind == "urlset":
            yield from (url for url in locations if is_product(url))
        else:
            raise ValueError(f"Неизвестный формат карты сайта: {sitemap}")


def normalized_text(element):
    return " ".join(element.text_content().split())


def read_price(value):
    match = re.fullmatch(r"([0-9][0-9\s]*(?:[.,][0-9]+)?)\s*(?:руб\.?|₽)?", str(value).strip())
    if match:
        return Decimal(re.sub(r"\s+", "", match[1]).replace(",", "."))
    return None


def parse_product(data, url):
    document = html.fromstring(data, parser=html.HTMLParser(encoding="utf-8"))
    products = document.xpath('//*[@itemtype="https://schema.org/Product"]')
    if not products:
        raise ValueError("На странице отсутствует карточка товара")
    product = products[0]
    props = [
        node.get(":product") for node in document.iter("store-availability") if node.get(":product")
    ]
    details = json.loads(props[0]) if props else {}
    headings = product.xpath(".//h1")
    name = normalized_text(headings[0]) if headings else details.get("name", "")
    articles = product.xpath('.//span[normalize-space(.)="Арт."]/following-sibling::span[1]')
    article = normalized_text(articles[0]) if articles else str(details.get("vendorCode") or "")

    breadcrumbs = document.xpath(
        '//*[contains(@itemtype, "/BreadcrumbList")]//*[@itemprop="itemListElement"]'
    )
    hierarchy = list(dict.fromkeys(
        label for node in breadcrumbs if (label := normalized_text(node))
        and label.casefold() not in {"главная", "каталог", name.casefold()}
    ))

    prices = product.xpath('.//*[@itemprop="price"]')
    current_price = read_price(normalized_text(prices[0])) if prices else None
    if current_price is None:
        current_price = read_price(details.get("price"))
    if not name or not article or current_price is None:
        raise ValueError("Не удалось прочитать название, артикул или текущую цену")
    old_nodes = product.xpath(
        './/*[contains(@class, "__aside-price-section")]'
        '//*[self::s or self::del or contains(@class, "__aside-price-old")]'
    )
    old_price = read_price(normalized_text(old_nodes[0])) if old_nodes else None
    if old_price is None:
        old_price = read_price(details.get("oldPrice"))
    if old_price is None or old_price <= current_price:
        old_price = ""

    sizes = [size.get("available") for size in details.get("sizes", [])]
    markers = product.xpath('.//*[@itemprop="availability"]/@href')
    out_of_stock = ""
    if any(flag is True for flag in sizes):
        out_of_stock = 0
    elif sizes and all(flag is False for flag in sizes):
        out_of_stock = 1
    elif not sizes and markers:
        if all(marker.endswith("/OutOfStock") for marker in markers):
            out_of_stock = 1
        elif any(marker.endswith("/InStock") for marker in markers):
            out_of_stock = 0

    return {
        "hierarchy": " > ".join(hierarchy),
        "name": name,
        "article": article,
        "current_price": current_price,
        "old_price": old_price,
        "out_of_stock": out_of_stock,
        "product_url": url,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("zolla.csv"))
    parser.add_argument("--limit", type=int, help="Максимум карточек для пробного запуска")
    parser.add_argument("--delay", type=float, default=0.5, help="Пауза перед каждым запросом, сек.")
    parser.add_argument("--url", action="append", help="Карточка Zolla. Можно передать несколько раз.")
    args = parser.parse_args()
    if args.delay < 0 or (args.limit is not None and args.limit < 1):
        parser.error("Требуется --delay >= 0 и --limit >= 1")
    if args.url and not all(map(is_product, args.url)):
        parser.error("--url должен указывать на карточку https://zolla.com/product/…")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    partial = args.output.with_name(args.output.name + ".partial")
    urls = args.url or discover_products(args.delay)
    seen = set()
    saved = failed = 0
    try:
        with partial.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS, delimiter=";")
            writer.writeheader()
            for url in urls:
                key = zolla_path(url).rstrip("/")
                if key in seen:
                    continue
                seen.add(key)
                try:
                    writer.writerow(parse_product(fetch(url, args.delay), url))
                    saved += 1
                except (URLError, TimeoutError, ValueError) as error:
                    failed += 1
                    logging.error("%s — %s", url, error)
                    if isinstance(error, HTTPError) and error.code in {401, 403, 429}:
                        break
                if len(seen) % 100 == 0:
                    logging.info("Сохранено %d, ошибок %d", saved, failed)
                if args.limit and len(seen) >= args.limit:
                    break
    except (URLError, TimeoutError, ValueError, ElementTree.ParseError) as error:
        logging.error("Не удалось прочитать карту сайта: %s", error)
        failed += 1
    if failed or not saved:
        logging.error("Сохранено %d, ошибок %d. Частичный результат: %s", saved, failed, partial)
        return 1
    partial.replace(args.output)
    logging.info("Сохранено %d карточек в %s", saved, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
