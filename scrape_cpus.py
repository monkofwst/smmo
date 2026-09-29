#!/usr/bin/env python3
"""Сбор характеристик PassMark в CSV. Python 3.10+, без сторонних библиотек.

Примеры:
    python scrape_cpus.py --limit 1500 --output cpus.csv
    python scrape_cpus.py --limit 20 --output example.csv
Повторный запуск продолжает существующий CSV; HTML сохраняется в кеше.
"""
import socket
import argparse
import csv
import hashlib
import random
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen


BASE_URL = "https://www.cpubenchmark.net"
CATALOG_URL = BASE_URL + "/cpu-list/all"
FIELDS = [
    "cpu_id", "name", "manufacturer", "category", "socket",
    "cores", "threads", "base_clock_ghz", "turbo_clock_ghz",
    "performance_cores", "efficiency_cores", "efficiency_base_clock_ghz",
    "efficiency_turbo_clock_ghz", "tdp_w", "l2_cache_mb", "l3_cache_mb",
    "l2_cache_raw", "l3_cache_raw", "first_seen_quarter", "first_seen_year",
    "cpu_mark", "single_thread_mark", "sample_count", "source_url",
    "scraped_at_utc",
]
REQUIRED = ["cores", "base_clock_ghz", "l3_cache_mb", "cpu_mark"]


class AccessBlocked(RuntimeError):
    """Дальнейшие запросы нужно остановить."""


class PageParser(HTMLParser):
    """Извлекает видимый текст, имя CPU и ссылки из строк каталога."""

    BREAKS = {"p", "div", "br", "tr", "td", "th", "h1", "h2", "li"}

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.cpus = {}
        self.cpu_row = None
        self.link = None
        self.name_parts = []
        self.in_name = False
        self.skip = None
        self.feed(source)
        self.close()
        self.text = "\n".join(
            line for raw in "".join(self.parts).splitlines()
            if (line := re.sub(r"\s+", " ", raw).strip())
        )
        self.name = " ".join("".join(self.name_parts).split())

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style"}:
            self.skip = tag
        if self.skip:
            return
        if tag in self.BREAKS:
            self.parts.append("\n")
        if tag == "span" and "cpuname" in attrs.get("class", "").split():
            self.in_name = True
        if tag == "tr":
            self.cpu_row = bool(re.fullmatch(r"cpu\d+", attrs.get("id", "")))
        if tag == "a" and self.cpu_row:
            href = attrs.get("href", "")
            parsed = urlparse(urljoin(BASE_URL, href))
            if (parsed.hostname == "www.cpubenchmark.net"
                    and parsed.path in {"/cpu_lookup.php", "/cpu.php"}):
                cpu_id = parse_qs(parsed.query).get("id", [""])[0]
                if cpu_id.isdigit():
                    # cpu_lookup.php ведёт на график; cpu.php — на карточку.
                    url = urlunparse(parsed._replace(path="/cpu.php", fragment=""))
                    self.link = [cpu_id, url, []]

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip:
                self.skip = None
            return
        if tag in self.BREAKS:
            self.parts.append("\n")
        if tag == "span":
            self.in_name = False
        if tag == "a" and self.link:
            cpu_id, url, parts = self.link
            name = " ".join("".join(parts).split())
            self.cpus.setdefault(cpu_id, {"cpu_id": cpu_id, "name": name, "source_url": url})
            self.link = None
        if tag == "tr":
            self.cpu_row = None

    def handle_data(self, data):
        if self.skip:
            return
        self.parts.append(data)
        if self.link:
            self.link[2].append(data)
        if self.in_name:
            self.name_parts.append(data)


def number(pattern, text, integer=False):
    match = re.search(pattern, text, re.I | re.M)
    if not match:
        return None
    value = match.group(1).replace(",", "").replace(" ", "")
    return int(value) if integer else float(value)


def label_value(text, label):
    match = re.search(r"^" + re.escape(label) + r"[ \t]*:[ \t]*([^\n]*)", text, re.M)
    return match.group(1).strip() if match else ""


def clock_ghz(value):
    match = re.search(r"([\d.]+)\s*(GHz|MHz)", value, re.I)
    if not match:
        return None
    return float(match.group(1)) / (1000 if match.group(2).lower() == "mhz" else 1)


def cache_mb(value):
    """Например: 6 x 512 KB -> 3 MB; неизвестный формат -> пустая ячейка."""
    match = re.fullmatch(r"(?:(\d+)\s*[x×]\s*)?([\d.]+)\s*(KB|MB|GB)", value, re.I)
    if not match:
        return None
    count, size, unit = match.groups()
    return int(count or 1) * float(size) * {"KB": 1 / 1024, "MB": 1, "GB": 1024}[unit.upper()]


def parse_cpu(source, item, fetched_at):
    page = PageParser(source)
    if not page.name or page.name.casefold() != item["name"].casefold():
        raise ValueError("Не найдена карточка ожидаемого процессора")
    text = page.text
    row = dict.fromkeys(FIELDS)
    row.update(item, scraped_at_utc=fetched_at)
    row["name"] = page.name
    row["manufacturer"] = next(
        (brand for brand in ("AMD", "Intel", "Apple", "Qualcomm", "Samsung")
         if page.name.startswith(brand + " ")), "Other"
    )
    for field, label in (("category", "Class"), ("socket", "Socket")):
        row[field] = label_value(text, label)

    row["cores"] = number(r"^(?:Total Cores|Cores):\s*(\d+)", text, True)
    row["threads"] = number(r"Threads:\s*(\d+)", text, True)
    if row["threads"] is None:
        row["threads"] = number(r"^Total Cores:\s*\d+\s+Cores,\s*(\d+)\s+Threads", text, True)
    row["base_clock_ghz"] = clock_ghz(label_value(text, "Clockspeed"))
    row["turbo_clock_ghz"] = clock_ghz(label_value(text, "Turbo Speed"))

    # У гибридных Intel отдельные частоты P- и E-ядер.
    for label, core_field, base_field, turbo_field in (
        ("Performance Cores", "performance_cores", "base_clock_ghz", "turbo_clock_ghz"),
        ("Efficient Cores", "efficiency_cores", "efficiency_base_clock_ghz", "efficiency_turbo_clock_ghz"),
    ):
        value = label_value(text, label)
        if value:
            row[core_field] = number(r"^(\d+)\s+Cores", value, True)
            row[base_field] = number(r"([\d.]+)\s+GHz\s+Base", value)
            row[turbo_field] = number(r"([\d.]+)\s+GHz\s+Turbo", value)

    row["tdp_w"] = number(r"^Typical TDP:\s*([\d.]+)\s*W", text)
    for level in (2, 3):
        raw = label_value(text, f"L{level} Cache")
        extra = label_value(text, f"Eff. L{level} Cache")
        amount = cache_mb(raw)
        if extra:
            extra_amount = cache_mb(extra)
            amount = amount + extra_amount if amount is not None and extra_amount is not None else None
        row[f"l{level}_cache_raw"] = " + ".join(v for v in (raw, extra) if v)
        row[f"l{level}_cache_mb"] = amount

    first_seen = label_value(text, "CPU First Seen on Charts")
    row["first_seen_quarter"] = number(r"Q([1-4])", first_seen, True)
    row["first_seen_year"] = number(r"\b((?:19|20)\d{2})\b", first_seen, True)
    row["cpu_mark"] = number(r"^Multithread Rating\s+([\d,]+)", text, True)
    row["single_thread_mark"] = number(r"^Single Thread Rating\s+([\d,]+)", text, True)
    row["sample_count"] = number(r"^Samples:\s*([\d,]+)", text, True)
    if not row["cpu_mark"]:
        raise ValueError("Не найден положительный CPU Mark; разметка могла измениться")
    return row


class Downloader:
    def __init__(self, cache_dir, delay, timeout):
        self.cache_dir = cache_dir
        self.delay = delay
        self.timeout = timeout
        self.last_request = 0.0
        cache_dir.mkdir(parents=True, exist_ok=True)

    def get(self, url):
        path = self.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".html")
        if path.exists():
            timestamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
            return path.read_text(encoding="utf-8"), timestamp

        for attempt in range(3):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            request = Request(url, headers={
                "User-Agent": "CpuLabCollector/1.0 (educational research)",
                "Accept": "text/html", "Accept-Language": "en-US,en;q=0.9",
            })
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    source = response.read().decode(response.headers.get_content_charset() or "utf-8", "replace")
                title = re.search(r"<title[^>]*>(.*?)</title>", source, re.I | re.S)
                if title and re.search(r"just a moment|access denied|captcha|attention required", title.group(1), re.I):
                    raise AccessBlocked("Сайт вернул проверку доступа. Сбор остановлен, прогресс сохранён.")
                temporary = path.with_suffix(".tmp")
                temporary.write_text(source, encoding="utf-8")
                temporary.replace(path)
                return source, datetime.now(timezone.utc).isoformat(timespec="seconds")
            except HTTPError as exc:
                if exc.code in (401, 403):
                    raise AccessBlocked(f"HTTP {exc.code}: сайт ограничил доступ. Повторите запуск позже.") from exc
                if exc.code not in (429, 500, 502, 503, 504):
                    raise
                pause = 2 ** (attempt + 1)
                if exc.code == 429:
                    value = exc.headers.get("Retry-After", "60")
                    try:
                        pause = max(pause, float(value))
                    except ValueError:
                        try:
                            pause = max(pause, parsedate_to_datetime(value).timestamp() - time.time())
                        except (ValueError, TypeError, OverflowError):
                            pause = 60
                if attempt == 2:
                    if exc.code == 429:
                        raise AccessBlocked("HTTP 429: лимит запросов. Повторите запуск позже.") from exc
                    raise
            except (URLError, TimeoutError, ConnectionError):
                if attempt == 2:
                    raise
                pause = 2 ** (attempt + 1)
            print(f"Повтор запроса через {pause:.0f} с: {url}", file=sys.stderr, flush=True)
            time.sleep(pause)


def existing_ids(path, delimiter, require_complete=False):
    if not path.exists() or path.stat().st_size == 0:
        return set()
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter=delimiter)
        if reader.fieldnames != FIELDS:
            raise ValueError("У CSV другой заголовок или разделитель. Укажите другое имя выходного файла.")
        ids = set()
        for row in reader:
            if (None in row or any(v is None for v in row.values())
                    or not row["cpu_id"].isdigit() or not row["source_url"] or not row["cpu_mark"]):
                raise ValueError("В CSV есть неполная строка. Исправьте её перед продолжением.")
            if row["cpu_id"] in ids:
                raise ValueError("В существующем CSV повторяется cpu_id")
            if require_complete and any(row[field] == "" for field in REQUIRED):
                raise ValueError("Существующий CSV содержит пропуски в обязательных факторах. Укажите другое имя файла для --require-complete.")
            ids.add(row["cpu_id"])
    return ids


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=1500, help="Число строк в итоговом CSV; 0 — весь каталог")
    parser.add_argument("--output", type=Path, default=Path("cpus.csv"))
    parser.add_argument("--cache-dir", type=Path, default=Path("passmark_cache"))
    parser.add_argument("--delay", type=float, default=1.0, help="Минимальный интервал между запросами, секунды")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--seed", type=int, default=42, help="Перемешивание каталога перед отбором")
    parser.add_argument("--delimiter", choices=[",", ";"], default=",")
    parser.add_argument("--require-complete", action="store_true", help="Только строки с ядрами, базовой частотой, L3 и CPU Mark")
    args = parser.parse_args(argv)
    if args.limit < 0 or args.delay < 0 or args.timeout <= 0:
        parser.error("limit и delay должны быть >= 0, timeout > 0")

    try:
        ids = existing_ids(args.output, args.delimiter, args.require_complete)
        if args.limit and len(ids) >= args.limit:
            print(f"В {args.output} уже {len(ids)} строк. Запросы не нужны.")
            return 0
        orig_getaddrinfo = socket.getaddrinfo

        def ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            return orig_getaddrinfo(
                    host, port,
                    socket.AF_INET,
                    type, proto, flags
                )

        socket.getaddrinfo = ipv4_only_getaddrinfo
        downloader = Downloader(args.cache_dir, args.delay, args.timeout)
        source, _ = downloader.get(CATALOG_URL)
        candidates = list(PageParser(source).cpus.values())
        if not candidates:
            raise ValueError("В каталоге не найдены ссылки на CPU. Проверьте доступ и разметку сайта.")
        random.Random(args.seed).shuffle(candidates)
        print(f"В каталоге: {len(candidates)} моделей; уже сохранено: {len(ids)}.", flush=True)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        need_header = not args.output.exists() or args.output.stat().st_size == 0
        failures = 0
        skipped = 0
        with args.output.open("a", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS, delimiter=args.delimiter)
            if need_header:
                writer.writeheader()
                stream.flush()
            for item in candidates:
                if item["cpu_id"] in ids:
                    continue
                try:
                    source, timestamp = downloader.get(item["source_url"])
                    row = parse_cpu(source, item, timestamp)
                except AccessBlocked:
                    raise
                except (URLError, TimeoutError, ConnectionError, ValueError) as exc:
                    failures += 1
                    print(f"Пропуск {item['name']}: {exc}", file=sys.stderr, flush=True)
                    if failures >= 5:
                        raise RuntimeError("Пять ошибок подряд. Проверьте соединение и разметку страниц.") from exc
                    continue
                failures = 0
                if args.require_complete and any(row[field] is None for field in REQUIRED):
                    skipped += 1
                    if skipped == 1 or skipped % 10 == 0:
                        print(f"Пропущено карточек с неполными факторами: {skipped}.", flush=True)
                    continue
                writer.writerow(row)
                stream.flush()
                ids.add(item["cpu_id"])
                target = args.limit or len(candidates)
                print(f"[{len(ids)}/{target}] {row['name']}: CPU Mark {row['cpu_mark']}", flush=True)
                if args.limit and len(ids) >= args.limit:
                    break
        print(f"Сохранено {len(ids)} строк: {args.output.resolve()}. Пропущено неполных: {skipped}.")
        if args.limit and len(ids) < args.limit:
            print("Каталог исчерпан: запрошенный объём не достигнут.", file=sys.stderr)
            return 1
        return 0
    except KeyboardInterrupt:
        print("\nОстановлено. Повторите ту же команду для продолжения.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Ошибка: {exc}\nСохранённые строки и кеш сохранены.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
