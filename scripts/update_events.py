#!/usr/bin/env python3
"""Weekly updater for index.html.

Pulls the RSS/Atom feeds listed in sources.json, keyword-filters items into
two buckets (play/activities vs schools/admissions), and splices a rendered
"What's New" block into index.html between the AUTO:START / AUTO:END markers.

Stdlib only (no pip install in CI). Each feed is fetched independently so one
dead feed never fails the run. Run `python3 scripts/update_events.py` to update;
`--selftest` runs offline logic checks.
"""
import html
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "sources.json"
INDEX = ROOT / "index.html"
START = "<!-- AUTO:START -->"
END = "<!-- AUTO:END -->"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15")

# Keyword buckets. Item goes to the first bucket that matches; SCHOOL is checked
# first because "kindergarten open day" is more specific than "family event".
# Early-years only: a toddler guide cares about kindergarten / nursery / playgroup,
# not primary/secondary. Generic terms (升學/入學/面試/叩門/admission) are excluded
# on purpose — they pull in K-12 news noise. KG open-day season is Sept–Dec.
SCHOOL_KW = [
    "kindergarten", "open day", "nursery", "playgroup", "pre-nursery", "pre nursery",
    "k1 ", "k1入", "n班", "n-class", "early years", "playgroups",
    "幼稚園", "幼兒園", "幼兒中心", "開放日", "學前", "遊戲小組", "親子班", "n班",
]
PLAY_KW = [
    "kid", "kids", "family", "families", "toddler", "children", "child-friendly",
    "playground", "play area", "weekend", "things to do", "what's on", "whats on",
    "event", "exhibition", "workshop", "half-term", "school holiday", "carnival",
    "親子", "好去處", "活動", "週末", "周末", "樂園", "工作坊", "展覽", "市集",
    "小朋友", "假期", "打卡",
]


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _text(el):
    return (el.text or "").strip() if el is not None else ""


def _strip_ns(tag):
    return tag.split("}", 1)[-1]


def parse_feed(data):
    """Return list of {title, link, summary, dt} for RSS or Atom bytes."""
    root = ET.fromstring(data)
    items = []
    # RSS: channel/item ; Atom: feed/entry
    nodes = [e for e in root.iter() if _strip_ns(e.tag) in ("item", "entry")]
    for node in nodes:
        title = link = summary = ""
        dt = None
        for child in node:
            tag = _strip_ns(child.tag)
            if tag == "title":
                title = _text(child)
            elif tag == "link":
                # RSS: element text; Atom: href attribute (prefer rel="alternate")
                if child.get("rel") == "alternate" and child.get("href"):
                    link = child.get("href")
                elif not link:
                    link = _text(child) or child.get("href", "")
            elif tag in ("description", "summary", "encoded") and not summary:
                summary = _text(child)
            elif tag in ("pubDate", "published", "updated", "date") and dt is None:
                dt = parse_date(_text(child))
        items.append({"title": title, "link": link, "summary": summary, "dt": dt})
    return items


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        return parsedate_to_datetime(s)  # RFC822 (RSS)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))  # ISO8601 (Atom)
    except ValueError:
        return None


def classify(title, summary):
    """Return 'school', 'play', or None."""
    blob = (title + " " + summary).lower()
    if any(k in blob for k in SCHOOL_KW):
        return "school"
    if any(k in blob for k in PLAY_KW):
        return "play"
    return None


def clean(text, limit=200):
    text = re.sub(r"<[^>]+>", "", text or "")       # strip HTML tags
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip() + ("…" if len(text) > limit else "")


def collect(cfg):
    recency = timedelta(days=cfg.get("recency_days", 60))
    cap = cfg.get("max_per_bucket", 10)
    now = datetime.now(timezone.utc)
    buckets = {"play": [], "school": []}
    seen = set()
    report = []
    for feed in cfg["feeds"]:
        try:
            items = parse_feed(fetch(feed["url"]))
        except Exception as e:  # noqa: BLE001 - isolate per feed
            report.append(f"DEAD  {feed['name']}: {e}")
            continue
        kept = 0
        for it in items:
            if not it["title"] or not it["link"]:
                continue
            dt = it["dt"]
            if dt is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if now - dt > recency:
                    continue
            bucket = classify(it["title"], it["summary"])
            if not bucket:
                continue
            key = it["link"].split("?")[0]
            if key in seen:
                continue
            seen.add(key)
            buckets[bucket].append({
                "title": clean(it["title"], 140),
                "link": it["link"],
                "summary": clean(it["summary"], 180),
                "source": feed["name"],
                "dt": dt,
            })
            kept += 1
        report.append(f"ok    {feed['name']}: {kept} kept / {len(items)} items")
    for b in buckets.values():
        b.sort(key=lambda x: x["dt"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        del b[cap:]
    return buckets, report


def render_cards(items):
    if not items:
        return '<p class="section-intro" style="font-size:.86rem;">No fresh items this week — check the sources below directly.</p>'
    out = ['<div class="card-grid">']
    for it in items:
        date = it["dt"].strftime("%d %b %Y") if it["dt"] else ""
        meta = f'<span>{html.escape(it["source"])}</span>' + (f'<span>{date}</span>' if date else "")
        desc = f'<p class="desc">{html.escape(it["summary"])}</p>' if it["summary"] else ""
        out.append(
            '<article class="card">'
            f'<h4>{html.escape(it["title"])}</h4>'
            f'<div class="meta">{meta}</div>'
            f'{desc}'
            f'<div class="card-link"><a href="{html.escape(it["link"])}" rel="noopener">Read ↗</a></div>'
            '</article>'
        )
    out.append("</div>")
    return "\n".join(out)


def render_watch(watch):
    lis = "\n".join(
        f'<li><a href="{html.escape(w["url"])}" rel="noopener">{html.escape(w["name"])}</a> '
        f'<span style="color:var(--ink-soft);font-size:.82rem;">— {html.escape(w["why"])}</span></li>'
        for w in watch
    )
    return f'<ul class="prose">\n{lis}\n</ul>'


def build_block(cfg, buckets):
    stamp = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    return "\n".join([
        START,
        f'<p class="section-intro" style="font-size:.82rem;">Auto-updated {stamp:%d %b %Y, %H:%M} HKT · pulled from the source list below.</p>',
        '<h3 style="font-size:1.05rem;margin-top:1.2rem;">Play &amp; activities</h3>',
        render_cards(buckets["play"]),
        '<h3 style="font-size:1.05rem;margin-top:1.5rem;">Schools &amp; admissions</h3>',
        render_cards(buckets["school"]),
        '<div class="callout" style="margin-top:1.5rem;"><h3>Watch these directly (not auto-scraped)</h3>'
        '<p class="section-intro" style="font-size:.84rem;">Login-walled or bot-blocked, so they can\'t be pulled automatically — the best of them, worth a weekly glance:</p>'
        + render_watch(cfg["watch"]) + '</div>',
        END,
    ])


def splice(html_text, block):
    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.DOTALL)
    if not pattern.search(html_text):
        raise SystemExit(f"Markers {START} / {END} not found in index.html")
    return pattern.sub(lambda _: block, html_text, count=1)


def main():
    cfg = json.loads(SOURCES.read_text(encoding="utf-8"))
    buckets, report = collect(cfg)
    print("\n".join(report), file=sys.stderr)
    block = build_block(cfg, buckets)
    updated = splice(INDEX.read_text(encoding="utf-8"), block)
    INDEX.write_text(updated, encoding="utf-8")
    n = len(buckets["play"]) + len(buckets["school"])
    print(f"Wrote {n} items ({len(buckets['play'])} play, {len(buckets['school'])} school) to index.html", file=sys.stderr)


def selftest():
    assert classify("Kindergarten open day this month", "") == "school"
    assert classify("Best things to do with kids this weekend", "") == "play"
    assert classify("New restaurant opens in Central", "") is None
    assert classify("幼稚園開放日", "") == "school"
    assert classify("親子好去處推介", "") == "play"
    assert clean("<p>Hi &amp; bye</p>") == "Hi & bye"
    assert parse_date("Fri, 03 Jul 2026 10:00:00 +0800") is not None
    assert parse_date("2026-07-03T10:00:00Z") is not None
    demo = f"x {START}\nold\n{END} y"
    assert splice(demo, f"{START}\nnew\n{END}") == f"x {START}\nnew\n{END} y"
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
