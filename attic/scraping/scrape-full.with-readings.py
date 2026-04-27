#!/usr/bin/env python3
"""
Generate an Old Style Orthodox calendar SQLite database from Holy Trinity's
simple calendar HTML endpoint.

Default usage:
    python scrape-full.py --year 2026 --output orthodox-calendar-2026.db

Offline/example usage:
    python scrape-full.py --html-files example-*.html --output examples.db

The database is normalised into:
    calendar_days       one row per Gregorian day
    saints              one row per saint/commemoration line
    service_ranks       typikon/service-rank mapping for jcal_img/*.gif icons
    scripture_readings  one row per scripture reading line
    hymns               one row per troparion/kontakion/exapostilarion paragraph

calendar_days also keeps convenience pipe-separated columns:
    all_saints, primary_saints, western_saints, scripture_readings, hymn_titles
"""

from __future__ import annotations

import argparse
import glob
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


@dataclass(frozen=True, slots=True)
class ServiceRank:
    """Typikon/service-rank meaning of one Holy Trinity jcal_img/*.gif icon."""

    icon_file: str
    code: str
    key: str
    name: str
    description: str
    sort_order: int


SERVICE_RANKS: dict[str, ServiceRank] = {
    "o.gif": ServiceRank(
        icon_file="o.gif",
        code="o",
        key="ordinary_minor",
        name="Ordinary / minor saint or event",
        description="Ordinary or minor saint/event day.",
        sort_order=0,
    ),
    "0.gif": ServiceRank(
        icon_file="0.gif",
        code="0",
        key="no_sign",
        name="No sign / without a sign",
        description=(
            "The most ordinary daily service to a saint: customarily three stikhera "
            "at 'Lord, I cry' and the canon at matins in four troparions; there may "
            "not be a troparion to the saint."
        ),
        sort_order=1,
    ),
    "1.gif": ServiceRank(
        icon_file="1.gif",
        code="1",
        key="no_sign",
        name="No sign / without a sign",
        description=(
            "The most ordinary daily service to a saint: customarily three stikhera "
            "at 'Lord, I cry' and the canon at matins in four troparions; there may "
            "not be a troparion to the saint. The source icon 1.gif is also used here "
            "to mark the primary commemoration line."
        ),
        sort_order=1,
    ),
    "2.gif": ServiceRank(
        icon_file="2.gif",
        code="2",
        key="six_verse",
        name="Six-verse / up to six",
        description=(
            "All six stikhera of 'Lord, I cry' are sung to the saint; there is a "
            "stikhera for 'Glory' of the aposticha for both vespers and matins, a "
            "troparion to the saint, and the matins canon is sung to the saint in six troparions."
        ),
        sort_order=2,
    ),
    "3.gif": ServiceRank(
        icon_file="3.gif",
        code="3",
        key="doxology",
        name="Doxology",
        description="Doxology-rank service.",
        sort_order=3,
    ),
    "4.gif": ServiceRank(
        icon_file="4.gif",
        code="4",
        key="polyeleos",
        name="Cross / Polyeleos",
        description=(
            "Polyeleos service: the Polyeleos/Praise/Magnification is sung at matins "
            "with Psalms 134 and 135 and verses; includes a Gospel reading, prokeimenon, "
            "gradual antiphons, canon with eight troparions, praises and Great Doxology; "
            "at vespers 'Blessed is the man' is sung, with entrance, Old Testament readings "
            "(parameia), and at lityia all verses may be sung to the saint."
        ),
        sort_order=4,
    ),
    "5.gif": ServiceRank(
        icon_file="5.gif",
        code="5",
        key="vigil",
        name="Vigil",
        description="Vigil-rank service.",
        sort_order=5,
    ),
    "6.gif": ServiceRank(
        icon_file="6.gif",
        code="6",
        key="great_feast_vigil",
        name="Vigil for great feasts",
        description="Vigil-rank service for great feasts.",
        sort_order=6,
    ),
}

UNKNOWN_SERVICE_RANK = ServiceRank(
    icon_file="",
    code="",
    key="unknown",
    name="Unknown",
    description="Unrecognised or missing service-rank icon.",
    sort_order=999,
)


def service_rank_for_icon_file(icon_file: str) -> ServiceRank:
    """Return the typikon/service-rank mapping for an icon filename."""
    if icon_file in SERVICE_RANKS:
        return SERVICE_RANKS[icon_file]
    return ServiceRank(
        icon_file=icon_file,
        code=Path(icon_file).stem if icon_file else "",
        key="unknown",
        name="Unknown",
        description="Unrecognised or missing service-rank icon.",
        sort_order=UNKNOWN_SERVICE_RANK.sort_order,
    )


@dataclass(slots=True)
class Saint:
    order: int
    text: str
    icon_file: str = ""
    icon_url: str = ""
    service_rank_code: str = ""
    service_rank_key: str = ""
    service_rank_name: str = ""
    service_rank_description: str = ""
    service_rank_sort_order: int = 999
    life_urls: list[str] = field(default_factory=list)
    is_primary: bool = False
    is_western: bool = False
    is_minor: bool = False
    raw_html: str = ""


@dataclass(slots=True)
class ScriptureReading:
    order: int
    verse_reference: str
    description: str = ""
    reading_url: str = ""
    text: str = ""
    raw_html: str = ""

    @property
    def display_text(self) -> str:
        if self.description:
            return f"{self.verse_reference} — {self.description}"
        return self.verse_reference


@dataclass(slots=True)
class Hymn:
    order: int
    section_order: int
    title: str
    hymn_type: str = ""
    tone: str = ""
    text: str = ""
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
    scripture_readings: list[ScriptureReading]
    hymns: list[Hymn]
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

    @property
    def scripture_readings_text(self) -> str:
        return " | ".join(r.display_text for r in self.scripture_readings)

    @property
    def hymn_titles(self) -> str:
        return " | ".join(h.title for h in self.hymns if h.title)


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
        service_rank = service_rank_for_icon_file(icon_file)
        saints.append(
            Saint(
                order=len(saints) + 1,
                text=text,
                icon_file=icon_file,
                icon_url=current_icon_url,
                service_rank_code=service_rank.code,
                service_rank_key=service_rank.key,
                service_rank_name=service_rank.name,
                service_rank_description=service_rank.description,
                service_rank_sort_order=service_rank.sort_order,
                life_urls=list(dict.fromkeys(current_urls)),
                is_primary=(icon_file == "1.gif"),
                is_western=saint_is_western(text, western_keywords),
                # Kept for backwards compatibility. Use service_rank_code/key for new code.
                is_minor=current_has_minortext or service_rank.code == "o",
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



def find_section_normaltext(soup: BeautifulSoup, header_class: str) -> Tag | None:
    """
    Find the <span class="normaltext"> immediately following a named section
    header such as scriptureheader or troparionheader.
    """
    header = soup.find("span", class_=header_class)
    if header is None:
        return None
    normal = header.find_next("span", class_="normaltext")
    return normal if isinstance(normal, Tag) else None


def extract_scripture_readings(soup: BeautifulSoup) -> list[ScriptureReading]:
    """Parse the optional scripture=1 readings section."""
    normal = find_section_normaltext(soup, "scriptureheader")
    if normal is None:
        return []

    readings: list[ScriptureReading] = []
    current_parts: list[str] = []
    current_raw: list[str] = []

    def finalise_current() -> None:
        nonlocal current_parts, current_raw
        raw_html = "".join(current_raw).strip()
        text = normalise_text(" ".join(current_parts))
        if not text:
            current_parts = []
            current_raw = []
            return

        raw_soup = BeautifulSoup(raw_html, "html.parser")
        first_anchor = raw_soup.find("a", href=True)
        if first_anchor is not None:
            verse_reference = normalise_text(first_anchor.get_text(" ", strip=False))
            reading_url = str(first_anchor.get("href", ""))
            description = text
            if verse_reference and description.startswith(verse_reference):
                description = normalise_text(description[len(verse_reference):])
        else:
            verse_reference = text
            reading_url = ""
            description = ""

        readings.append(
            ScriptureReading(
                order=len(readings) + 1,
                verse_reference=verse_reference,
                description=description,
                reading_url=reading_url,
                text=text,
                raw_html=raw_html,
            )
        )
        current_parts = []
        current_raw = []

    for child in normal.children:
        if isinstance(child, Tag) and child.name == "br":
            finalise_current()
            continue
        if isinstance(child, Tag):
            current_raw.append(str(child))
            current_parts.append(child.get_text(" ", strip=False))
        else:
            current_raw.append(str(child))
            current_parts.append(str(child))

    finalise_current()
    return readings


def clean_hymn_heading(value: str) -> str:
    """Normalise a hymn heading and strip trailing dash punctuation."""
    value = normalise_text(value)
    value = re.sub(r"\s*[—-]\s*$", "", value)
    return value.strip()


def extract_tone(title: str) -> str:
    """Extract a tone marker from a hymn heading, when present."""
    match = re.search(
        r"\b(?:in\s+)?Tone\s+([IVXLCDM]+|\d+|[A-Za-z]+)",
        title,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else ""


def extract_hymn_type(title: str) -> str:
    lower = title.lower()
    if "kontakion" in lower:
        return "Kontakion"
    if "troparion" in lower or "troparia" in lower:
        return "Troparion"
    if "exapostilarion" in lower:
        return "Exapostilarion"
    if "theotokion" in lower:
        return "Theotokion"
    if "sticher" in lower:
        return "Sticheron"
    return ""


def parse_hymn_paragraph(paragraph: Tag, *, order: int, section_order: int) -> Hymn | None:
    """Parse a single <p> from the optional trp=1 Troparia section."""
    raw_html = str(paragraph).strip()
    bold_texts = [b.get_text(" ", strip=False) for b in paragraph.find_all("b")]
    title = clean_hymn_heading(" ".join(bold_texts))

    paragraph_copy = BeautifulSoup(str(paragraph), "html.parser").find("p")
    if paragraph_copy is None:
        return None
    for bold in paragraph_copy.find_all("b"):
        bold.decompose()
    body = normalise_text(paragraph_copy.get_text(" ", strip=False))

    if not title and not body:
        return None

    return Hymn(
        order=order,
        section_order=section_order,
        title=title,
        hymn_type=extract_hymn_type(title),
        tone=extract_tone(title),
        text=body,
        raw_html=raw_html,
    )


def extract_hymns(soup: BeautifulSoup) -> list[Hymn]:
    """
    Parse the optional trp=1 Troparia section into one row per paragraph.

    Separator tables are used by the source page to divide groups belonging to
    different commemorations, so section_order is incremented at each separator.
    """
    normal = find_section_normaltext(soup, "troparionheader")
    if normal is None:
        return []

    hymns: list[Hymn] = []
    section_order = 1

    for child in normal.children:
        if not isinstance(child, Tag):
            continue
        if child.name == "table" and child.find(class_="troparionseparator") is not None:
            section_order += 1
            continue
        if child.name != "p":
            continue
        hymn = parse_hymn_paragraph(
            child,
            order=len(hymns) + 1,
            section_order=section_order,
        )
        if hymn is not None:
            hymns.append(hymn)

    return hymns

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
    scripture_readings = extract_scripture_readings(soup)
    hymns = extract_hymns(soup)

    return CalendarDay(
        dataheader=dataheader,
        gregorian_date=gregorian,
        gregorian_weekday=weekday,
        julian_date=julian,
        headerheader=headerheader,
        fasting_class=fasting_class,
        fasting_rule=fasting_rule,
        saints=saints,
        scripture_readings=scripture_readings,
        hymns=hymns,
        source_url=source_url,
        fetched_at=fetched_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def build_url(
    day: date,
    *,
    include_troparia: bool = True,
    include_scripture: bool = True,
) -> str:
    return (
        f"{BASE_URL}?month={day:%m}&today={day:%d}&year={day:%Y}"
        f"&dt=1&header=1&lives=1&trp={int(include_troparia)}&scripture={int(include_scripture)}"
    )


def fetch_html(
    session: requests.Session,
    day: date,
    *,
    cache_dir: Path | None = None,
    timeout: int = 30,
    include_troparia: bool = True,
    include_scripture: bool = True,
) -> tuple[str, str]:
    url = build_url(
        day,
        include_troparia=include_troparia,
        include_scripture=include_scripture,
    )
    cache_path = (
        cache_dir / f"{day.isoformat()}-trp{int(include_troparia)}-scr{int(include_scripture)}.html"
        if cache_dir
        else None
    )
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
            scripture_readings TEXT NOT NULL DEFAULT '',
            hymn_titles TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS service_ranks (
            icon_file TEXT PRIMARY KEY,
            code TEXT NOT NULL UNIQUE,
            rank_key TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            sort_order INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS saints (
            id INTEGER PRIMARY KEY,
            day_id INTEGER NOT NULL REFERENCES calendar_days(id) ON DELETE CASCADE,
            saint_order INTEGER NOT NULL,
            name TEXT NOT NULL,
            icon_file TEXT NOT NULL,
            icon_url TEXT NOT NULL,
            service_rank_code TEXT NOT NULL,
            service_rank_key TEXT NOT NULL,
            service_rank_name TEXT NOT NULL,
            service_rank_description TEXT NOT NULL,
            service_rank_sort_order INTEGER NOT NULL,
            life_urls TEXT NOT NULL,
            is_primary INTEGER NOT NULL DEFAULT 0,
            is_western INTEGER NOT NULL DEFAULT 0,
            is_minor INTEGER NOT NULL DEFAULT 0,
            raw_html TEXT NOT NULL,
            UNIQUE(day_id, saint_order),
            FOREIGN KEY(service_rank_code) REFERENCES service_ranks(code)
        );

        CREATE TABLE IF NOT EXISTS scripture_readings (
            id INTEGER PRIMARY KEY,
            day_id INTEGER NOT NULL REFERENCES calendar_days(id) ON DELETE CASCADE,
            reading_order INTEGER NOT NULL,
            verse_reference TEXT NOT NULL,
            description TEXT NOT NULL,
            reading_url TEXT NOT NULL,
            display_text TEXT NOT NULL,
            raw_html TEXT NOT NULL,
            UNIQUE(day_id, reading_order)
        );

        CREATE TABLE IF NOT EXISTS hymns (
            id INTEGER PRIMARY KEY,
            day_id INTEGER NOT NULL REFERENCES calendar_days(id) ON DELETE CASCADE,
            hymn_order INTEGER NOT NULL,
            section_order INTEGER NOT NULL,
            hymn_type TEXT NOT NULL,
            tone TEXT NOT NULL,
            title TEXT NOT NULL,
            text TEXT NOT NULL,
            raw_html TEXT NOT NULL,
            UNIQUE(day_id, hymn_order)
        );

        CREATE TABLE app_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_scripture_readings_day_id ON scripture_readings(day_id);
        CREATE INDEX IF NOT EXISTS idx_scripture_readings_reference ON scripture_readings(verse_reference);
        CREATE INDEX IF NOT EXISTS idx_hymns_day_id ON hymns(day_id);
        CREATE INDEX IF NOT EXISTS idx_hymns_type ON hymns(hymn_type);
        CREATE INDEX IF NOT EXISTS idx_hymns_tone ON hymns(tone);

        CREATE INDEX IF NOT EXISTS idx_calendar_days_julian_date ON calendar_days(julian_date);
        CREATE INDEX IF NOT EXISTS idx_saints_day_id ON saints(day_id);
        CREATE INDEX IF NOT EXISTS idx_saints_primary ON saints(is_primary);
        CREATE INDEX IF NOT EXISTS idx_saints_western ON saints(is_western);
        CREATE INDEX IF NOT EXISTS idx_saints_service_rank_code ON saints(service_rank_code);
        CREATE INDEX IF NOT EXISTS idx_saints_service_rank_sort_order ON saints(service_rank_sort_order);
        CREATE INDEX IF NOT EXISTS idx_saints_name ON saints(name);
        """
    )

    # Support --append against databases created by earlier versions of this script.
    existing_calendar_columns = {
        row[1] for row in con.execute("PRAGMA table_info(calendar_days)").fetchall()
    }
    calendar_day_column_migrations = {
        "scripture_readings": "TEXT NOT NULL DEFAULT ''",
        "hymn_titles": "TEXT NOT NULL DEFAULT ''",
    }
    for column_name, column_def in calendar_day_column_migrations.items():
        if column_name not in existing_calendar_columns:
            con.execute(f"ALTER TABLE calendar_days ADD COLUMN {column_name} {column_def}")

    existing_saint_columns = {
        row[1] for row in con.execute("PRAGMA table_info(saints)").fetchall()
    }
    saint_column_migrations = {
        "service_rank_code": "TEXT NOT NULL DEFAULT ''",
        "service_rank_key": "TEXT NOT NULL DEFAULT ''",
        "service_rank_name": "TEXT NOT NULL DEFAULT ''",
        "service_rank_description": "TEXT NOT NULL DEFAULT ''",
        "service_rank_sort_order": "INTEGER NOT NULL DEFAULT 999",
    }
    for column_name, column_def in saint_column_migrations.items():
        if column_name not in existing_saint_columns:
            con.execute(f"ALTER TABLE saints ADD COLUMN {column_name} {column_def}")

    con.executemany(
        """
        INSERT INTO service_ranks (icon_file, code, rank_key, name, description, sort_order)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(icon_file) DO UPDATE SET
            code=excluded.code,
            rank_key=excluded.rank_key,
            name=excluded.name,
            description=excluded.description,
            sort_order=excluded.sort_order
        """,
        [
            (
                rank.icon_file,
                rank.code,
                rank.key,
                rank.name,
                rank.description,
                rank.sort_order,
            )
            for rank in SERVICE_RANKS.values()
        ],
    )
    return con


def upsert_day(con: sqlite3.Connection, day: CalendarDay) -> int:
    con.execute(
        """
        INSERT INTO calendar_days (
            gregorian_date, gregorian_year, gregorian_month, gregorian_day, gregorian_weekday,
            julian_date, julian_year, julian_month, julian_day,
            dataheader, headerheader, fasting_class, fasting_rule,
            all_saints, primary_saints, western_saints, scripture_readings, hymn_titles,
            source_url, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            scripture_readings=excluded.scripture_readings,
            hymn_titles=excluded.hymn_titles,
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
            day.scripture_readings_text,
            day.hymn_titles,
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
            day_id, saint_order, name, icon_file, icon_url,
            service_rank_code, service_rank_key, service_rank_name,
            service_rank_description, service_rank_sort_order, life_urls,
            is_primary, is_western, is_minor, raw_html
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                day_id,
                saint.order,
                saint.text,
                saint.icon_file,
                saint.icon_url,
                saint.service_rank_code,
                saint.service_rank_key,
                saint.service_rank_name,
                saint.service_rank_description,
                saint.service_rank_sort_order,
                " | ".join(saint.life_urls),
                int(saint.is_primary),
                int(saint.is_western),
                int(saint.is_minor),
                saint.raw_html,
            )
            for saint in day.saints
        ],
    )

    con.execute("DELETE FROM scripture_readings WHERE day_id = ?", (day_id,))
    con.executemany(
        """
        INSERT INTO scripture_readings (
            day_id, reading_order, verse_reference, description, reading_url,
            display_text, raw_html
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                day_id,
                reading.order,
                reading.verse_reference,
                reading.description,
                reading.reading_url,
                reading.display_text,
                reading.raw_html,
            )
            for reading in day.scripture_readings
        ],
    )

    con.execute("DELETE FROM hymns WHERE day_id = ?", (day_id,))
    con.executemany(
        """
        INSERT INTO hymns (
            day_id, hymn_order, section_order, hymn_type, tone, title, text, raw_html
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                day_id,
                hymn.order,
                hymn.section_order,
                hymn.hymn_type,
                hymn.tone,
                hymn.title,
                hymn.text,
                hymn.raw_html,
            )
            for hymn in day.hymns
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
        if any(ch in pattern for ch in "*?["):
            matches = [Path(match) for match in sorted(glob.glob(pattern))]
        else:
            matches = [Path(pattern)]
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
        "--no-scripture",
        action="store_true",
        help="Do not request or parse scripture readings; default requests scripture=1",
    )
    parser.add_argument(
        "--no-troparia",
        action="store_true",
        help="Do not request or parse troparia/kontakia; default requests trp=1",
    )
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
    scripture_count = 0
    hymn_count = 0
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
                    scripture_count += len(day.scripture_readings)
                    hymn_count += len(day.hymns)
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
                        include_scripture=not args.no_scripture,
                        include_troparia=not args.no_troparia,
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
                    scripture_count += len(day.scripture_readings)
                    hymn_count += len(day.hymns)
                    print(
                        f"[{index:03d}/{total:03d}] {day.gregorian_date} "
                        f"saints={len(day.saints)} primary={sum(s.is_primary for s in day.saints)} "
                        f"western={sum(s.is_western for s in day.saints)} "
                        f"scripture={len(day.scripture_readings)} hymns={len(day.hymns)}"
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
        f"primary saint rows={primary_count}, western saint rows={western_count}, "
        f"scripture reading rows={scripture_count}, hymn rows={hymn_count}"
    )
    if errors:
        print("\nErrors:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
