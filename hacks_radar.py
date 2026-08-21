#!/usr/bin/env python3
"""
hacks_radar.py — слой 3. САМЫЙ ВАЖНЫЙ. Опережающий индикатор.

Логика: крупный взлом → аналитики (Chainalysis/TRM/Elliptic) метят кластер
за часы → хакер отмывает через миксеры и instant-обменники за 1–3 суток →
горячие кошельки этих обменников тухнут → твой вывод оттуда получает
косвенную экспозицию → биржа тормозит депозит.

То есть между взломом и «покраской» твоего кошелька есть окно в 24–72 часа.
Именно его этот скрипт и ловит. OFAC до того же события дойдёт через месяцы.

Источники:
  1. DefiLlama /hacks — структурировано, но с лагом в дни. Подтверждение.
  2. RSS-ленты (rekt.news и т.п.) — быстрее.
  3. Для минутной скорости нужны X-аккаунты PeckShield / SlowMist / Cyvers.
     У X нет бесплатного API — прокидывай через RSSHub / rss.app и клади
     получившийся URL в config.rss_feeds.

Запуск: раз в 15–30 минут.
"""

import json
import re
import sys
import time
import xml.etree.ElementTree as ET

from common import (
    cfg, esc, http, load_state, now_iso, raise_wave, save_state, tg, tg_lines,
)

HACKS_URLS = ["https://api.llama.fi/hacks", "https://api.llama.fi/api/hacks"]
STATE = "hacks_seen.json"


# ------------------------------------------------------------- DefiLlama


def fetch_hacks():
    for url in HACKS_URLS:
        try:
            body, _, _ = http(url, timeout=60)
            data = json.loads(body)
            if isinstance(data, dict):
                data = data.get("hacks") or data.get("data") or []
            if isinstance(data, list) and data:
                return data
        except Exception:
            continue
    return []


def usd(item):
    """DefiLlama отдаёт amount то в млн, то в долларах. Нормализуем эвристикой."""
    a = item.get("amount") or item.get("amountLost") or 0
    try:
        a = float(a)
    except Exception:
        return 0.0
    return a * 1_000_000 if 0 < a < 100_000 else a


def hack_id(item):
    return str(
        item.get("id")
        or f"{item.get('name','?')}|{item.get('date','?')}"
    )


def scan_defillama(threshold):
    seen = load_state(STATE, {"llama": [], "rss": []})
    known = set(seen.get("llama", []))
    items = fetch_hacks()
    if not items:
        return []

    fresh, ids = [], []
    for it in items:
        hid = hack_id(it)
        ids.append(hid)
        if hid in known:
            continue
        if usd(it) < threshold:
            continue
        fresh.append(it)

    # Первый запуск: только запоминаем, не спамим историей за 10 лет.
    first_run = not known
    seen["llama"] = ids[-3000:]
    save_state(STATE, seen)
    return [] if first_run else fresh


# ------------------------------------------------------------- RSS


AMOUNT_RE = re.compile(r"\$\s?([\d.,]+)\s?(m|mm|million|b|bn|billion|k)\b", re.I)


def parse_feed(xml_bytes):
    out = []
    root = ET.fromstring(xml_bytes)
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        rec = {}
        for c in node:
            ct = c.tag.rsplit("}", 1)[-1]
            if ct == "title":
                rec["title"] = (c.text or "").strip()
            elif ct in ("link",):
                rec["link"] = (c.get("href") or c.text or "").strip()
            elif ct in ("pubDate", "updated", "published"):
                rec["date"] = (c.text or "").strip()
            elif ct in ("guid", "id"):
                rec["id"] = (c.text or "").strip()
        if rec.get("title"):
            rec.setdefault("id", rec.get("link", rec["title"]))
            out.append(rec)
    return out


def scan_rss():
    c = cfg()
    feeds = c.get("rss_feeds", [])
    kws = [k.lower() for k in c.get("rss_keywords", [])]
    if not feeds:
        return []

    seen = load_state(STATE, {"llama": [], "rss": []})
    known = set(seen.get("rss", []))
    first_run = not known
    fresh, all_ids = [], []

    for url in feeds:
        try:
            body, _, _ = http(url, timeout=45)
            for rec in parse_feed(body):
                all_ids.append(rec["id"])
                if rec["id"] in known:
                    continue
                text = (rec.get("title", "")).lower()
                if kws and not any(k in text for k in kws):
                    continue
                rec["feed"] = url
                fresh.append(rec)
        except Exception as e:
            print(f"[rss] {url}: {e}")
        time.sleep(1)

    seen["rss"] = (list(known) + all_ids)[-3000:]
    save_state(STATE, seen)
    return [] if first_run else fresh


# ------------------------------------------------------------- main


def main():
    threshold = cfg().get("hack_alert_threshold_usd", 5_000_000)
    lines = []

    hacks = scan_defillama(threshold)
    if hacks:
        lines.append(f"🚨 <b>Новые инциденты (DefiLlama, ≥${threshold:,.0f})</b>")
        lines.append(
            "<i>Окно 24–72ч: средства пойдут через миксеры и обменники. "
            "Выводы через instant-свопы в эти сутки — на паузу.</i>"
        )
        lines.append("")
        for h in hacks[:25]:
            chains = ", ".join(h.get("chain") or h.get("chains") or [])
            lines.append(
                f"• <b>{esc(h.get('name', '?'))}</b> — ${usd(h):,.0f}"
                + (f"  [{esc(chains)}]" if chains else "")
            )
            tech = h.get("technique") or h.get("classification")
            if tech:
                lines.append(f"   {esc(tech)}")
            if h.get("source"):
                lines.append(f"   {esc(h['source'])}")
        raise_wave("hack", f"{len(hacks)} инцидентов ≥${threshold:,.0f}", len(hacks))

    news = scan_rss()
    if news:
        lines.append("")
        lines.append(f"📰 <b>Лента: {len(news)} совпадений</b>")
        for n in news[:20]:
            lines.append(f"• {esc(n['title'][:180])}")
            if n.get("link"):
                lines.append(f"   {esc(n['link'])}")
        raise_wave("news", f"{len(news)} новостей по ключевым словам", len(news))

    if lines:
        tg_lines(lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
