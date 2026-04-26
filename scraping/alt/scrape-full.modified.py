#!/usr/bin/env python3
"""
Generate an Old Style Orthodox calendar SQLite database from Holy Trinity's
simple calendar HTML endpoint.

Default usage:
    python scrape-full.py --year 2026 --output orthodox-calendar-2026.db

Offline/example usage:
    python scrape-full.py --html-files example-*.html --output examples.db

The database is normalised into:
    calendar_days  one row per Gregorian day
    saints         one row per saint/commemoration line

calendar_days also keeps convenience pipe-separated columns:
    all_saints, primary_saints, western_saints
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://www.holytrinityorthodox.com/calendar/calendar2.php"
DEFAULT_WESTERN_KEYWORDS = (
    # Explicit tags used by the source site.
    "Celtic & British",
    "Celtic",
    "British",
    # Useful fallbacks for untagged British/Irish saints.
    "Britain",
    "England",
    "English",
    "East Anglia",
    "Scotland",
    "Scottish",
    "Scots",
    "Ireland",
    "Irish",
    "Wales",
    "Welsh",
    "Cornwall",
    "Cornish",
    "Manx",
    "Isle of Man",
    "Iona",
    "Lindisfarne",
    "Buchan",
    "Coventry",
    "Clonard",
    "Glendalough",
    "Skellig",
)


@dataclass(slots=True)
class Saint:
    order: int
    text: str
    icon_file: str = ""
    icon_url: str = ""
    life_urls: list[str] = field(default_factory=list)
    is_primary: bool = False
    is_western: bool = False
    is_minor: bool = False
    raw_html: str = ""


@dataclass(slots=True)
class CalendarDay:
    dataheader: str
    gregorian_date: date
    gregorian_weekday: str
    julian_date: date
    headerheader: str
    fasting_class: str
    fasting_rule: str
    saints: list[Saint]
    source_url: str
    fetched_at: str

    @property
    def all_saints(self) -> str:
        return " | ".join(s.text for s in self.saints)

    @property
    def primary_saints(self) -> str:
        return " | ".join(s.text for s in self.saints if s.is_primary)

    @property
    def western_saints(self) -> str:
        return " | ".join(s.text for s in self.saints if s.is_western)


def normalise_text(value: str) -> str:
    """Collapse source HTML whitespace without damaging readable punctuation."""
    value = html.unescape(value or "").replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"\s+([,.;:!?)\]])", r"\1", value)
    value = re.sub(r"([(\[])\s+", r"\1", value)
    value = re.sub(r"\b(\d+)\s+(st|nd|rd|th)\b", r"\1\2", value, flags=re.I)
    return value


def parse_calendar_date(dataheader: str) -> tuple[date, str, date]:
    """
    Parse strings such as:
        Friday December 25, 2026 / December 12, 2026
    into Gregorian date, Gregorian weekday, Julian date.
    """
    cleaned = normalise_text(dataheader)
    match = re.match(
        r"^(?P<weekday>\w+)\s+"
        r"(?P<g_month>[A-Za-z]+)\s+(?P<g_day>\d{1,2}),\s+(?P<g_year>\d{4})\s*/\s*"
        r"(?P<j_month>[A-Za-z]+)\s+(?P<j_day>\d{1,2}),\s+(?P<j_year>\d{4})$",
        cleaned,
    )
    if not match:
        raise ValueError(f"Could not parse dataheader: {dataheader!r}")

    gregorian = datetime.strptime(
        f"{match['g_month']} {match['g_day']} {match['g_year']}", "%B %d %Y"
    ).date()
    julian = datetime.strptime(
        f"{match['j_month']} {match['j_day']} {match['j_year']}", "%B %d %Y"
    ).date()
    return gregorian, match["weekday"], julian


def extract_header(header_span: Tag | None) -> tuple[str, str, str]:
    """Return headerheader text, fasting class, fasting rule."""
    if header_span is None:
        return "", "", ""

    header_copy = BeautifulSoup(str(header_span), "html.parser").find("span", class_="headerheader")
    if header_copy is None:
        return "", "", ""

    fasting_span = header_copy.find("span", class_=re.compile(r"\b(headerfast|headernofast)\b"))
    fasting_class = ""
    fasting_rule = ""
    if fasting_span is not None:
        classes = fasting_span.get("class", [])
        fasting_class = next((c for c in classes if c in {"headerfast", "headernofast"}), "")
        fasting_rule = normalise_text(fasting_span.get_text(" ", strip=False))
        fasting_span.extract()

    header_text = normalise_text(header_copy.get_text(" ", strip=False))
    return header_text, fasting_class, fasting_rule


def basename_from_url(url: str) -> str:
    parsed = urlparse(url or "")
    return os.path.basename(parsed.path) if parsed.path else ""


def saint_is_western(text: str, keywords: Sequence[str]) -> bool:
    lower = text.lower()
    return any(keyword.lower() in lower for keyword in keywords)


def iter_anchor_urls(node: Tag) -> list[str]:
    urls: list[str] = []
    if node.name == "a" and node.get("href"):
        urls.append(str(node["href"]))
    for anchor in node.find_all("a", href=True):
        urls.append(str(anchor["href"]))
    return urls


def extract_saints(soup: BeautifulSoup, western_keywords: Sequence[str]) -> list[Saint]:
    """
    Parse each saint/commemoration line by using each jcal_img/*.gif image as
    the beginning of a row and the next <br> as its end.
    """
    normal = soup.find("span", class_="normaltext")
    if normal is None:
        return []

    saints: list[Saint] = []
    current_icon_url = ""
    current_icon_file = ""
    current_parts: list[str] = []
    current_raw: list[str] = []
    current_urls: list[str] = []
    current_has_minortext = False

    def finalise_current() -> None:
        nonlocal current_icon_url, current_icon_file, current_parts, current_raw, current_urls, current_has_minortext
        if not current_icon_url:
            return
        text = normalise_text(" ".join(current_parts))
        if not text:
            current_icon_url = ""
            current_icon_file = ""
            current_parts = []
            current_raw = []
            current_urls = []
            current_has_minortext = False
            return

        icon_file = current_icon_file
        saints.append(
            Saint(
                order=len(saints) + 1,
                text=text,
                icon_file=icon_file,
                icon_url=current_icon_url,
                life_urls=list(dict.fromkeys(current_urls)),
                is_primary=(icon_file == "1.gif"),
                is_western=saint_is_western(text, western_keywords),
                is_minor=current_has_minortext or icon_file == "o.gif",
                raw_html="".join(current_raw).strip(),
            )
        )
        current_icon_url = ""
        current_icon_file = ""
        current_parts = []
        current_raw = []
        current_urls = []
        current_has_minortext = False

    for child in normal.children:
        if isinstance(child, Tag) and child.name == "img":
            finalise_current()
            current_icon_url = str(child.get("src", ""))
            current_icon_file = basename_from_url(current_icon_url)
            current_raw = [str(child)]
            current_parts = []
            current_urls = []
            current_has_minortext = False
            continue

        if not current_icon_url:
            # Ignore section headings such as "February 29th." which are not
            # themselves saint rows.
            continue

        if isinstance(child, Tag) and child.name == "br":
            finalise_current()
            continue

        if isinstance(child, Tag):
            current_raw.append(str(child))
            current_parts.append(child.get_text(" ", strip=False))
            current_urls.extend(iter_anchor_urls(child))
            classes = child.get("class", [])
            if "minortext" in classes or child.find(class_="minortext") is not None:
                current_has_minortext = True
        else:
            current_raw.append(str(child))
            current_parts.append(str(child))

    finalise_current()
    return saints


def parse_calendar_html(
    html_text: str,
    *,
    source_url: str,
    western_keywords: Sequence[str] = DEFAULT_WESTERN_KEYWORDS,
    fetched_at: str | None = None,
) -> CalendarDay:
    soup = BeautifulSoup(html_text, "html.parser")

    dataheader_span = soup.find("span", class_="dataheader")
    if dataheader_span is None:
        raise ValueError(f"Missing span.dataheader in {source_url}")
    dataheader = normalise_text(dataheader_span.get_text(" ", strip=False))
    gregorian, weekday, julian = parse_calendar_date(dataheader)

    headerheader, fasting_class, fasting_rule = extract_header(soup.find("span", class_="headerheader"))
    saints = extract_saints(soup, western_keywords)

    return CalendarDay(
        dataheader=dataheader,
        gregorian_date=gregorian,
        gregorian_weekday=weekday,
        julian_date=julian,
        headerheader=headerheader,
        fasting_class=fasting_class,
        fasting_rule=fasting_rule,
        saints=saints,
        source_url=source_url,
        fetched_at=fetched_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def build_url(day: date) -> str:
    return (
        f"{BASE_URL}?month={day:%m}&today={day:%d}&year={day:%Y}"
        "&dt=1&header=1&lives=1&trp=0&scripture=0"
    )


def fetch_html(
    session: requests.Session,
    day: date,
    *,
    cache_dir: Path | None = None,
    timeout: int = 30,
) -> tuple[str, str]:
    url = build_url(day)
    cache_path = cache_dir / f"{day.isoformat()}.html" if cache_dir else None
    if cache_path and cache_path.exists():
        return cache_path.read_text(encoding="utf-8"), url

    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    response.encoding = response.encoding or "utf-8"
    text = response.text
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(text, encoding="utf-8")
    return text, url


def connect_db(path: Path, *, append: bool = False) -> sqlite3.Connection:
    if path.exists() and not append:
        path.unlink()
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS calendar_days (
            id INTEGER PRIMARY KEY,
            gregorian_date TEXT NOT NULL UNIQUE,
            gregorian_year INTEGER NOT NULL,
            gregorian_month INTEGER NOT NULL,
            gregorian_day INTEGER NOT NULL,
            gregorian_weekday TEXT NOT NULL,
            julian_date TEXT NOT NULL,
            julian_year INTEGER NOT NULL,
            julian_month INTEGER NOT NULL,
            julian_day INTEGER NOT NULL,
            dataheader TEXT NOT NULL,
            headerheader TEXT NOT NULL,
            fasting_class TEXT NOT NULL,
            fasting_rule TEXT NOT NULL,
            all_saints TEXT NOT NULL,
            primary_saints TEXT NOT NULL,
            western_saints TEXT NOT NULL,
            source_url TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS saints (
            id INTEGER PRIMARY KEY,
            day_id INTEGER NOT NULL REFERENCES calendar_days(id) ON DELETE CASCADE,
            saint_order INTEGER NOT NULL,
            name TEXT NOT NULL,
            icon_file TEXT NOT NULL,
            icon_url TEXT NOT NULL,
            life_urls TEXT NOT NULL,
            is_primary INTEGER NOT NULL DEFAULT 0,
            is_western INTEGER NOT NULL DEFAULT 0,
            is_minor INTEGER NOT NULL DEFAULT 0,
            raw_html TEXT NOT NULL,
            UNIQUE(day_id, saint_order)
        );

        CREATE INDEX IF NOT EXISTS idx_calendar_days_julian_date ON calendar_days(julian_date);
        CREATE INDEX IF NOT EXISTS idx_saints_day_id ON saints(day_id);
        CREATE INDEX IF NOT EXISTS idx_saints_primary ON saints(is_primary);
        CREATE INDEX IF NOT EXISTS idx_saints_western ON saints(is_western);
        CREATE INDEX IF NOT EXISTS idx_saints_name ON saints(name);
        """
    )
    return con


def upsert_day(con: sqlite3.Connection, day: CalendarDay) -> int:
    con.execute(
        """
        INSERT INTO calendar_days (
            gregorian_date, gregorian_year, gregorian_month, gregorian_day, gregorian_weekday,
            julian_date, julian_year, julian_month, julian_day,
            dataheader, headerheader, fasting_class, fasting_rule,
            all_saints, primary_saints, western_saints, source_url, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(gregorian_date) DO UPDATE SET
            gregorian_year=excluded.gregorian_year,
            gregorian_month=excluded.gregorian_month,
            gregorian_day=excluded.gregorian_day,
            gregorian_weekday=excluded.gregorian_weekday,
            julian_date=excluded.julian_date,
            julian_year=excluded.julian_year,
            julian_month=excluded.julian_month,
            julian_day=excluded.julian_day,
            dataheader=excluded.dataheader,
            headerheader=excluded.headerheader,
            fasting_class=excluded.fasting_class,
            fasting_rule=excluded.fasting_rule,
            all_saints=excluded.all_saints,
            primary_saints=excluded.primary_saints,
            western_saints=excluded.western_saints,
            source_url=excluded.source_url,
            fetched_at=excluded.fetched_at
        """,
        (
            day.gregorian_date.isoformat(),
            day.gregorian_date.year,
            day.gregorian_date.month,
            day.gregorian_date.day,
            day.gregorian_weekday,
            day.julian_date.isoformat(),
            day.julian_date.year,
            day.julian_date.month,
            day.julian_date.day,
            day.dataheader,
            day.headerheader,
            day.fasting_class,
            day.fasting_rule,
            day.all_saints,
            day.primary_saints,
            day.western_saints,
            day.source_url,
            day.fetched_at,
        ),
    )
    day_id = int(
        con.execute(
            "SELECT id FROM calendar_days WHERE gregorian_date = ?", (day.gregorian_date.isoformat(),)
        ).fetchone()[0]
    )
    con.execute("DELETE FROM saints WHERE day_id = ?", (day_id,))
    con.executemany(
        """
        INSERT INTO saints (
            day_id, saint_order, name, icon_file, icon_url, life_urls,
            is_primary, is_western, is_minor, raw_html
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                day_id,
                saint.order,
                saint.text,
                saint.icon_file,
                saint.icon_url,
                " | ".join(saint.life_urls),
                int(saint.is_primary),
                int(saint.is_western),
                int(saint.is_minor),
                saint.raw_html,
            )
            for saint in day.saints
        ],
    )
    return day_id


def parse_iso_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def daterange(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def default_year_range(year: int) -> tuple[date, date]:
    return date(year, 1, 1), date(year, 12, 31)


def read_html_files(paths: Sequence[str]) -> Iterable[tuple[str, str]]:
    for pattern in paths:
        matches = sorted(Path().glob(pattern)) if any(ch in pattern for ch in "*?[") else [Path(pattern)]
        for path in matches:
            yield path.read_text(encoding="utf-8"), str(path)


def make_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate an Old Style Orthodox calendar SQLite database."
    )
    parser.add_argument("--year", type=int, default=datetime.now().year, help="Gregorian year to scrape")
    parser.add_argument("--start-date", type=parse_iso_date, help="First Gregorian date, YYYY-MM-DD")
    parser.add_argument("--end-date", type=parse_iso_date, help="Last Gregorian date, YYYY-MM-DD")
    parser.add_argument("--output", type=Path, default=None, help="SQLite database path")
    parser.add_argument("--append", action="store_true", help="Append/update instead of recreating the DB")
    parser.add_argument("--html-files", nargs="*", help="Parse local HTML files instead of fetching URLs")
    parser.add_argument("--cache-dir", type=Path, help="Cache fetched HTML files here")
    parser.add_argument("--delay", type=float, default=0.15, help="Delay between HTTP requests")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds")
    parser.add_argument(
        "--western-keyword",
        action="append",
        default=[],
        help="Extra keyword for western/British/Celtic saint detection; may be repeated",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = make_arg_parser().parse_args(argv)
    output = args.output or Path(f"{args.year}.db")
    western_keywords = tuple(dict.fromkeys((*DEFAULT_WESTERN_KEYWORDS, *args.western_keyword)))
    con = connect_db(output, append=args.append)

    parsed_count = 0
    saint_count = 0
    primary_count = 0
    western_count = 0
    errors: list[str] = []

    try:
        if args.html_files:
            sources = read_html_files(args.html_files)
            for html_text, source in sources:
                try:
                    day = parse_calendar_html(
                        html_text,
                        source_url=source,
                        western_keywords=western_keywords,
                    )
                    upsert_day(con, day)
                    parsed_count += 1
                    saint_count += len(day.saints)
                    primary_count += sum(s.is_primary for s in day.saints)
                    western_count += sum(s.is_western for s in day.saints)
                except Exception as exc:  # noqa: BLE001 - CLI should report and continue.
                    errors.append(f"{source}: {exc}")
        else:
            start, end = (
                (args.start_date, args.end_date)
                if args.start_date or args.end_date
                else default_year_range(args.year)
            )
            if start is None:
                start = date(args.year, 1, 1)
            if end is None:
                end = date(args.year, 12, 31)
            if end < start:
                raise SystemExit("--end-date must not be earlier than --start-date")

            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": (
                        "orthodox-calendar-scraper/1.0 "
                        "(+https://www.holytrinityorthodox.com/calendar/)"
                    )
                }
            )
            total = (end - start).days + 1
            for index, current_day in enumerate(daterange(start, end), start=1):
                try:
                    html_text, source_url = fetch_html(
                        session,
                        current_day,
                        cache_dir=args.cache_dir,
                        timeout=args.timeout,
                    )
                    day = parse_calendar_html(
                        html_text,
                        source_url=source_url,
                        western_keywords=western_keywords,
                    )
                    upsert_day(con, day)
                    parsed_count += 1
                    saint_count += len(day.saints)
                    primary_count += sum(s.is_primary for s in day.saints)
                    western_count += sum(s.is_western for s in day.saints)
                    print(
                        f"[{index:03d}/{total:03d}] {day.gregorian_date} "
                        f"saints={len(day.saints)} primary={sum(s.is_primary for s in day.saints)} "
                        f"western={sum(s.is_western for s in day.saints)}"
                    )
                    if args.delay > 0 and index < total:
                        time.sleep(args.delay)
                except Exception as exc:  # noqa: BLE001 - report all failed dates.
                    errors.append(f"{current_day.isoformat()}: {exc}")
        con.commit()
    finally:
        con.close()

    print(f"Wrote {output}")
    print(
        f"Parsed days={parsed_count}, saints={saint_count}, "
        f"primary saint rows={primary_count}, western saint rows={western_count}"
    )
    if errors:
        print("\nErrors:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
