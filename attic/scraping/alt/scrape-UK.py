#!/usr/bin/env python
# coding: utf-8

## Orthodox Calendar scraper – holytrinityorthodox.com
## Produces a SQLite database (2026.db) with two tables:
##   calendar  – one row per Gregorian date
##   saints    – one row per saint, FK'd back to calendar

## Example URL:
# https://www.holytrinityorthodox.com/calendar/calendar2.php?month=04&today=26&year=2026&dt=1&header=1&lives=1&trp=0&scripture=0

from bs4 import BeautifulSoup
import requests
import pandas as pd
from datetime import datetime, timedelta
import sqlite3
import re
import warnings
from tqdm import tqdm

warnings.filterwarnings('ignore')

# ── configuration ────────────────────────────────────────────────────────────
YEAR      = 2026
DB_PATH   = "2026-UK-claude.db"
BASE_URL  = ("https://www.holytrinityorthodox.com/calendar/calendar2.php"
             "?month={month}&today={day}&year={year}"
             "&dt=1&header=1&lives=1&trp=0&scripture=0")

# gif filenames that mark primary (high-rank) feasts / saints
PRIMARY_GIFS = {"1.gif", "2.gif"}

# ── helpers ───────────────────────────────────────────────────────────────────

def gif_name(img_tag):
    """Return just the filename portion of an <img> src, e.g. '1.gif'."""
    src = img_tag.get("src", "")
    return src.rsplit("/", 1)[-1]


def parse_tone(header_text):
    """Extract 'Tone N' from the headerheader text, or '' if absent."""
    m = re.search(r'Tone\s+\w+', header_text, re.IGNORECASE)
    return m.group(0) if m else ""


def parse_liturgical_week(header_text):
    """Everything in headerheader before the tone / before the <br>."""
    # Strip the tone portion so we get a clean liturgical-week string
    text = re.sub(r'\s*Tone\s+\w+\.?\s*', '', header_text).strip().strip('.')
    return text


def extract_saint_text(node):
    """
    Given the BeautifulSoup node that follows an <img>, return clean plain text
    for that saint entry (may span an <a>, a <span class='minortext'>, etc.).
    """
    parts = []
    sibling = node
    while sibling:
        if hasattr(sibling, 'name'):
            if sibling.name == 'img':        # next saint's image → stop
                break
            if sibling.name == 'br':
                break
            if sibling.name in ('b',):       # Julian-date sub-header → stop
                break
            parts.append(sibling.get_text())
        else:
            text = str(sibling)
            if '\n' in text and not text.strip():
                pass
            else:
                parts.append(text)
        sibling = sibling.next_sibling
    return ' '.join(parts).replace('\xa0', ' ').strip()


def is_celtic_british(text):
    return 'Celtic' in text or 'British' in text


def parse_saints(normaltext_span):
    """
    Walk the normaltext span and return a list of dicts:
      { name, is_primary, is_western }
    """
    saints = []
    children = list(normaltext_span.children)
    i = 0
    while i < len(children):
        node = children[i]
        # We only start a saint entry on an <img> tag
        if hasattr(node, 'name') and node.name == 'img':
            gif = gif_name(node)
            # Collect text from following siblings until next <img> or <br>/<b>
            text_parts = []
            j = i + 1
            while j < len(children):
                sib = children[j]
                if hasattr(sib, 'name'):
                    if sib.name == 'img':
                        break
                    if sib.name == 'br':
                        j += 1          # consume the <br>
                        break
                    if sib.name == 'b': # Julian sub-header line
                        break
                    text_parts.append(sib.get_text())
                else:
                    raw = str(sib)
                    text_parts.append(raw)
                j += 1
            i = j  # advance outer cursor

            saint_text = ' '.join(text_parts).replace('\xa0', ' ').strip()
            saint_text = re.sub(r'\s+', ' ', saint_text).strip()
            if not saint_text:
                continue

            saints.append({
                'name':       saint_text,
                'is_primary': gif in PRIMARY_GIFS,
                'is_western': is_celtic_british(saint_text),
            })
        else:
            i += 1
    return saints


# ── main loop ─────────────────────────────────────────────────────────────────

calendar_rows = []
saint_rows    = []

start_date = datetime(YEAR, 1, 1)
days_in_year = 366 if (YEAR % 4 == 0 and (YEAR % 100 != 0 or YEAR % 400 == 0)) else 365

for i in tqdm(range(days_in_year), desc="Scraping"):
    current_date = start_date + timedelta(days=i)
    day_str      = current_date.strftime("%d")
    month_str    = current_date.strftime("%m")
    greg_date    = current_date.strftime("%Y-%m-%d")

    url  = BASE_URL.format(month=month_str, day=day_str, year=YEAR)
    page = requests.get(url, timeout=15)
    soup = BeautifulSoup(page.content, "html.parser")

    # ── dataheader ──────────────────────────────────────────────────────────
    dataheader_tag = soup.find('span', class_='dataheader')
    dataheader_text = dataheader_tag.text if dataheader_tag else ''

    parts = dataheader_text.split('/')
    greg_date_display = " ".join(parts[0].split()[1:]).strip() if parts else ''
    julian_date       = parts[1].strip() if len(parts) > 1 else ''

    # ── headerheader ────────────────────────────────────────────────────────
    headerheader_tag = soup.find('span', class_='headerheader')
    if headerheader_tag:
        # Get text of headerheader *excluding* the nested fast span
        # so we don't bleed fasting text into the liturgical description.
        hh_clone = BeautifulSoup(str(headerheader_tag), 'html.parser')
        for child in hh_clone.find_all('span'):
            child.decompose()
        headerheader_text = hh_clone.get_text(' ', strip=True)
    else:
        headerheader_text = ''

    liturgical_week = parse_liturgical_week(headerheader_text)
    tone            = parse_tone(headerheader_text)

    # ── fasting rule (headerfast OR headernofast) ────────────────────────────
    fast_tag = soup.find('span', class_='headerfast')
    if fast_tag is None:
        fast_tag = soup.find('span', class_='headernofast')
    fast_rule = fast_tag.text.strip() if fast_tag else ''

    # ── saints ───────────────────────────────────────────────────────────────
    normaltext_span = soup.find('span', class_='normaltext')
    if normaltext_span:
        day_saints = parse_saints(normaltext_span)
    else:
        day_saints = []

    # ── accumulate ───────────────────────────────────────────────────────────
    calendar_rows.append({
        'greg_date':          greg_date,
        'greg_date_display':  greg_date_display,
        'julian_date':        julian_date,
        'liturgical_week':    liturgical_week,
        'tone':               tone,
        'fast_rule':          fast_rule,
    })

    for s in day_saints:
        saint_rows.append({
            'greg_date':  greg_date,
            'name':       s['name'],
            'is_primary': int(s['is_primary']),
            'is_western': int(s['is_western']),
        })

# ── build DataFrames ─────────────────────────────────────────────────────────

cal_df    = pd.DataFrame(calendar_rows)
saints_df = pd.DataFrame(saint_rows)

# ── write to SQLite ──────────────────────────────────────────────────────────

con = sqlite3.connect(DB_PATH)
cur = con.cursor()

cur.executescript("""
    DROP TABLE IF EXISTS saints;
    DROP TABLE IF EXISTS calendar;

    CREATE TABLE calendar (
        greg_date         TEXT PRIMARY KEY,   -- ISO 8601, e.g. 2026-04-26
        greg_date_display TEXT,               -- "April 26, 2026"
        julian_date       TEXT,               -- "April 13, 2026"
        liturgical_week   TEXT,               -- "Third Sunday of Pascha: The Myrrh-bearing Women"
        tone              TEXT,               -- "Tone two"
        fast_rule         TEXT                -- "Fish Allowed" / "Strict Fast …" / ""
    );

    CREATE TABLE saints (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        greg_date   TEXT NOT NULL REFERENCES calendar(greg_date),
        name        TEXT NOT NULL,
        is_primary  INTEGER NOT NULL DEFAULT 0,  -- 1 = red-letter / 1.gif or 2.gif saint
        is_western  INTEGER NOT NULL DEFAULT 0   -- 1 = Celtic & British saint
    );

    CREATE INDEX idx_saints_date     ON saints(greg_date);
    CREATE INDEX idx_saints_primary  ON saints(greg_date, is_primary);
    CREATE INDEX idx_saints_western  ON saints(greg_date, is_western);
""")

cal_df.to_sql('calendar', con, if_exists='append', index=False)
saints_df.to_sql('saints',   con, if_exists='append', index=False)

con.commit()
con.close()

print(f"\nDone. {len(cal_df)} calendar days and {len(saints_df)} saint entries written to {DB_PATH}")
