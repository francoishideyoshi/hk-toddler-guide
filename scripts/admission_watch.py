#!/usr/bin/env python3
"""Daily admission-year watcher.

Fetches the two kindergarten admission pages, extracts the school year that is
currently accepting applications (e.g. 2026/2027), and sends a Telegram message
EVERY day — even when nothing changed. When the open year advances (2026/2027 ->
2027/2028) the message flags it and lists the live application-form links pulled
straight off the page.

State (last-seen year per site) lives in data/admission_state.json so change
detection survives across daily runs; the workflow commits it back.

Stdlib only (no pip in CI). Env:
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (required to actually send)
Run:
    python3 scripts/admission_watch.py            # fetch + notify + save state
    python3 scripts/admission_watch.py --dry-run  # fetch + print, don't send/save
    python3 scripts/admission_watch.py --selftest  # offline logic checks
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "admission_state.json"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15")

# link_re: pulls the live application-form URLs off each page so the message
# always points at the current forms, even after the school swaps them yearly.
SITES = [
    {
        "name": "Learning Habitat 學之園 (PN/K1)",
        "url": "https://www.learninghabitat.org/en/admission/online-application",
        "link_re": r"https?://[^\"'\s>]*jotform\.com/\d+",
    },
    {
        "name": "Think International Kindergarten",
        "url": "https://www.think.edu.hk/admission2023",
        "link_re": r"https?://[^\"'\s>]*cloudoase\.com[^\"'\s>]*",
    },
]

# A HK school year is a consecutive YYYY/YYYY (or YYYY/YY) slash pair. The slash
# is what separates a real school year from date-range noise like "2020 - 31".
YEAR_RE = re.compile(r"20(\d{2})\s*/\s*(?:20)?(\d{2})")


def fetch(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def extract_years(text: str) -> list[str]:
    """Return normalized consecutive-year strings, e.g. ['2026/2027']."""
    out: set[str] = set()
    for m in YEAR_RE.finditer(text):
        y1 = 2000 + int(m.group(1))
        y2 = 2000 + int(m.group(2))
        if y2 == y1 + 1:  # drop non-consecutive junk (phones, id ranges)
            out.add(f"{y1}/{y2}")
    return sorted(out)


def current_year(years: list[str]) -> str | None:
    """Newest open year = the one with the largest start year."""
    return max(years, key=lambda y: int(y[:4])) if years else None


def extract_links(text: str, pattern: str, cap: int = 6) -> list[str]:
    seen: list[str] = []
    for u in re.findall(pattern, text):
        if u not in seen:
            seen.append(u)
        if len(seen) >= cap:
            break
    return seen


def check_site(site: dict[str, str]) -> dict[str, object]:
    try:
        text = fetch(site["url"])
    except Exception as e:  # noqa: BLE001 - one dead site must not kill the run
        return {"name": site["name"], "url": site["url"], "error": str(e)}
    years = extract_years(text)
    return {
        "name": site["name"],
        "url": site["url"],
        "year": current_year(years),
        "years": years,
        "links": extract_links(text, site["link_re"]),
    }


def load_state() -> dict[str, dict]:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {}


def save_state(results: list[dict]) -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    state = {
        r["name"]: {"year": r.get("year"), "links": r.get("links", []), "checked": stamp}
        for r in results
        if "error" not in r
    }
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def build_message(results: list[dict], prev: dict[str, dict]) -> tuple[str, bool]:
    """Return (HTML message, any_change)."""
    hkt = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    lines = [f"📚 <b>HK Kindergarten Admission Watch</b>\n<i>{hkt:%a %d %b %Y}</i>"]
    any_change = False

    for r in results:
        name = html.escape(r["name"])
        src = html.escape(r["url"])
        if "error" in r:
            last = prev.get(r["name"], {}).get("year")
            note = f"last known {html.escape(last)}" if last else "no data yet"
            lines.append(
                f"\n⚠️ <b>{name}</b>\nCouldn't check today ({note}).\n"
                f'<a href="{src}">Open page ↗</a>'
            )
            continue

        year = r.get("year")
        before = prev.get(r["name"], {}).get("year")
        changed = before is not None and year != before
        if changed:
            any_change = True
            head = (f"🔔 <b>CHANGED — {name}</b>\nOpen year: "
                    f"{html.escape(before or '?')} → <b>{html.escape(year or '?')}</b>")
        else:
            open_txt = html.escape(year) if year else "none detected on page"
            head = f"\n🏫 <b>{name}</b>\nCurrently open: <b>{open_txt}</b>"
        lines.append(head)

        if r.get("links"):
            links = " · ".join(
                f'<a href="{html.escape(u)}">Apply {i}</a>'
                for i, u in enumerate(r["links"], 1)
            )
            lines.append(f"Forms: {links}")
        lines.append(f'<a href="{src}">Admission page ↗</a>')

    lines.append(
        "\n" + ("🔔 Change detected above — check the new form links."
                if any_change else "No change since last check.")
    )
    return "\n".join(lines), any_change


def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise SystemExit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
    data = urllib.parse.urlencode({
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=25) as r:
        if r.status != 200:
            raise SystemExit(f"Telegram HTTP {r.status}: {r.read()[:200]!r}")


def main(dry_run: bool = False) -> None:
    prev = load_state()
    results = [check_site(s) for s in SITES]
    message, _ = build_message(results, prev)
    if dry_run:
        print(message)
        return
    send_telegram(message)
    save_state(results)
    print("Sent Telegram message and saved state.", file=sys.stderr)


def selftest() -> None:
    assert extract_years("PN 2026/27 and K1 2026/2027") == ["2026/2027"]
    assert extract_years("born 2020 - 31 Dec, phone 2074-2079") == []
    assert extract_years("2026/2027 then 2027/2028") == ["2026/2027", "2027/2028"]
    assert current_year(["2026/2027", "2027/2028"]) == "2027/2028"
    assert current_year([]) is None
    assert extract_links(
        'a https://www.jotform.com/111 b https://www.jotform.com/111 c https://www.jotform.com/222',
        r"https?://[^\"'\s>]*jotform\.com/\d+",
    ) == ["https://www.jotform.com/111", "https://www.jotform.com/222"]
    # change detection: prev year differs -> flagged
    res = [{"name": "S", "url": "u", "year": "2027/2028", "years": ["2027/2028"], "links": []}]
    _, changed = build_message(res, {"S": {"year": "2026/2027"}})
    assert changed is True
    _, same = build_message(res, {"S": {"year": "2027/2028"}})
    assert same is False
    # first-ever run (no prev) must NOT flag as change
    _, first = build_message(res, {})
    assert first is False
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main(dry_run="--dry-run" in sys.argv)
