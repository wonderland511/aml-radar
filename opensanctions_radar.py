#!/usr/bin/env python3
"""
opensanctions_radar.py — слой 2. Все юрисдикции разом.

OpenSanctions консолидирует ~450 источников: OFAC, ЕС, UK OFSI, Япония,
Канада, Украина (НАЗК/СНБО), Израиль (NBCTF), FBI Lazarus, ransomwhe.re.
Крипто-адреса лежат как сущности схемы CryptoWallet.

Это существенно раньше OFAC: ЕС и Украина часто публикуют адреса первыми.

Лицензия: бесплатно для некоммерческого использования, для бизнеса нужна
лицензия на данные — https://www.opensanctions.org/licensing/

Запуск: раз в 6 часов (чаще бессмысленно, экспорты обновляются реже).
"""

import json
import sys
import urllib.request

from common import (
    UA, esc, key, load_state, now_iso, raise_wave, save_state, tg, tg_lines,
)

DATA = "https://data.opensanctions.org/datasets/latest/{ds}/entities.ftm.json"
INDEX = "https://data.opensanctions.org/datasets/{ds}/latest/index.json"

# 'sanctions' — сводная коллекция всех санкционных списков мира.
# Остальные — узкоспециальные крипто-датасеты.
DATASETS = [
    "sanctions",
    "us_fbi_lazarus_crypto",
    "ransomwhere",
    "il_mod_crypto",
]

STATE = "opensanctions_addrs.json"
VERSIONS = "opensanctions_versions.json"


def dataset_version(ds):
    try:
        req = urllib.request.Request(INDEX.format(ds=ds), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            meta = json.loads(r.read())
        return meta.get("version") or meta.get("last_export") or ""
    except Exception:
        return ""


def stream_wallets(ds):
    """
    Файл может весить сотни МБ — стримим построчно и НЕ держим в памяти.
    Байтовый префильтр до json.loads даёт разницу в разы.
    """
    req = urllib.request.Request(DATA.format(ds=ds), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=900) as r:
        for raw in r:
            if b'"CryptoWallet"' not in raw:
                continue
            try:
                e = json.loads(raw)
            except Exception:
                continue
            if e.get("schema") != "CryptoWallet":
                continue
            p = e.get("properties", {})
            pubkeys = p.get("publicKey") or []
            currencies = p.get("currency") or ["?"]
            holders = p.get("holder") or p.get("holderName") or []
            for pk in pubkeys:
                yield {
                    "asset": str(currencies[0]).upper(),
                    "addr": pk,
                    "name": str(holders[0])[:120] if holders else "(unnamed)",
                    "topics": ",".join(p.get("topics") or []),
                    "datasets": ",".join(e.get("datasets") or [ds])[:200],
                    "source_ds": ds,
                    "seen": now_iso(),
                }


def main():
    versions = load_state(VERSIONS)
    current, touched = {}, []

    for ds in DATASETS:
        ver = dataset_version(ds)
        try:
            for w in stream_wallets(ds):
                current[key(w["asset"], w["addr"])] = w
            if ver and ver != versions.get(ds):
                touched.append(ds)
            versions[ds] = ver or versions.get(ds, "")
        except Exception as e:
            tg(f"⚠️ <b>OpenSanctions</b>: датасет {esc(ds)} не скачался — {esc(e)}")
            return 1  # не перезаписываем снапшот на неполных данных

    if not current:
        tg("⚠️ <b>OpenSanctions</b>: 0 кошельков. Проверь схему/URL — формат мог измениться.")
        return 1

    old = load_state(STATE)

    if not old:
        save_state(STATE, current)
        save_state(VERSIONS, versions)
        tg(
            f"✅ OpenSanctions-радар поднят. Baseline: <b>{len(current)}</b> "
            f"адресов из {len(DATASETS)} датасетов."
        )
        return 0

    added = [current[k] for k in current if k not in old]
    removed = [old[k] for k in old if k not in current]

    if added:
        spread = {}
        for a in added:
            spread[a["asset"]] = spread.get(a["asset"], 0) + 1
        lines = [
            f"🟠 <b>Мировые санкции: +{len(added)} адресов</b>  ("
            + ", ".join(f"{k}×{v}" for k, v in sorted(spread.items()))
            + ")",
            f"<i>Датасеты: {esc(', '.join(touched) or 'n/a')}</i>",
            "",
        ]
        for a in added[:80]:
            lines.append(f"<b>{a['asset']}</b> <code>{esc(a['addr'])}</code>")
            lines.append(f"   {esc(a['name'])} — {esc(a['datasets'][:90])}")
        if len(added) > 80:
            lines.append(f"…и ещё {len(added) - 80}")
        if removed:
            lines.append(f"\n🟢 Снято: {len(removed)}")
        tg_lines(lines)
        raise_wave("opensanctions", f"+{len(added)} адресов", len(added))
    elif removed:
        tg(f"🟢 <b>OpenSanctions</b>: снято {len(removed)} адресов.")

    save_state(STATE, current)
    save_state(VERSIONS, versions)
    return 0


if __name__ == "__main__":
    sys.exit(main())
