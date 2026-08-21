#!/usr/bin/env python3
"""
ofac_radar.py — слой 1. Официальный список США.
Запаздывающий сигнал: подтверждает, что волна была. Держим ради полноты базы.

Тянет SDN.XML + CONSOLIDATED.XML из Sanctions List Service, вытаскивает
Digital Currency Address, диффит со снапшотом, шлёт добавленные и удалённые.

Запуск: раз в час.
"""

import re
import sys
import xml.etree.ElementTree as ET

from common import (
    cfg, esc, http_cached, key, load_state, now_iso, raise_wave, save_state,
    tg, tg_lines,
)

BASE = "https://sanctionslistservice.ofac.treas.gov/api/download"
FILES = ["SDN.XML", "CONSOLIDATED.XML"]
STATE = "ofac_addrs.json"

# "Digital Currency Address - XBT 1A1zP..." | "- ETH 0xabc..." | "- XMR 4..."
ADDR_RE = re.compile(
    r"Digital Currency Address\s*[-\u2010-\u2015]\s*([A-Za-z0-9]{2,8})\s+"
    r"([A-Za-z0-9:_.\-]{20,140})"
)


def local(tag):
    """'{ns}sdnEntry' -> 'sdnEntry'. OFAC уже дважды менял namespace."""
    return tag.rsplit("}", 1)[-1]


def extract(xml_bytes, source):
    found = {}
    root = ET.fromstring(xml_bytes)
    for entry in root.iter():
        if local(entry.tag) != "sdnEntry":
            continue
        f = {local(c.tag): (c.text or "") for c in entry}
        remarks = f.get("remarks", "")
        if "Digital Currency Address" not in remarks:
            continue

        name = " ".join(
            x for x in (f.get("firstName", ""), f.get("lastName", "")) if x
        ).strip()
        programs = ", ".join(
            p.text for p in entry.iter() if local(p.tag) == "program" and p.text
        )

        for asset, addr in ADDR_RE.findall(remarks):
            found[key(asset, addr)] = {
                "asset": asset.upper(),
                "addr": addr,
                "name": name or "(unnamed)",
                "uid": f.get("uid", ""),
                "programs": programs,
                "list": source,
                "seen": now_iso(),
            }
    return found


def main():
    current = {}
    for fname in FILES:
        try:
            body, _ = http_cached(f"{BASE}/{fname}", f"ofac_{fname}")
            current.update(extract(body, fname))
        except Exception as e:
            tg(f"⚠️ <b>OFAC-радар</b>: не смог обработать {esc(fname)} — {esc(e)}")
            return 1  # снапшот НЕ трогаем: иначе на след. запуске будет фантомная «волна»

    if not current:
        tg(
            "⚠️ <b>OFAC-радар</b>: распарсили 0 адресов.\n"
            "Это не «изменений нет», это скорее всего сломанный парсер — "
            "OFAC поменял формат. Чинить ADDR_RE."
        )
        return 1

    old = load_state(STATE)

    if not old:
        save_state(STATE, current)
        tg(f"✅ OFAC-радар поднят. Baseline: <b>{len(current)}</b> крипто-адресов.")
        return 0

    added = [current[k] for k in current if k not in old]
    removed = [old[k] for k in old if k not in current]

    if added or removed:
        lines = []
        if added:
            spread = {}
            for a in added:
                spread[a["asset"]] = spread.get(a["asset"], 0) + 1
            lines.append(
                f"🔴 <b>OFAC: +{len(added)} крипто-адресов</b>  ("
                + ", ".join(f"{k}×{v}" for k, v in sorted(spread.items()))
                + ")"
            )
            lines.append("<i>Формальная волна. Прогони AML по своим и по обменникам.</i>")
            lines.append("")
            for a in added[:80]:
                lines.append(f"<b>{a['asset']}</b> <code>{esc(a['addr'])}</code>")
                lines.append(f"   {esc(a['name'])} — {esc(a['programs'])}")
            if len(added) > 80:
                lines.append(f"…и ещё {len(added) - 80}")
            raise_wave("ofac", f"+{len(added)} адресов в SDN/Consolidated", len(added))

        if removed:
            lines.append("")
            lines.append(f"🟢 <b>Делистинг: −{len(removed)}</b>")
            for a in removed[:30]:
                lines.append(
                    f"<b>{a['asset']}</b> <code>{esc(a['addr'])}</code> — {esc(a['name'])}"
                )

        tg_lines(lines)

    save_state(STATE, current)
    return 0


if __name__ == "__main__":
    sys.exit(main())
