#!/usr/bin/env python3
"""
exposure_watch.py — слой 4. Персональный. Твоя главная защита.

Две задачи:

  A) ОБМЕННИКИ. Смотрит входящие на горячие кошельки обменников, которыми ты
     пользуешься, и сверяет отправителей с грязным множеством (OFAC +
     OpenSanctions + твой локальный блеклист + миксеры).
     Приток из помеченных кластеров = обменник тухнет в ближайшие дни.
     Вывод: выводить через него сейчас нельзя.

  B) СВОИ КОШЕЛЬКИ. Прямое входящее с грязного адреса — красный алерт.
     Прямая экспозиция это худший вид, любой скоринг её видит сразу.

Где брать адреса горячих кошельков обменника: посмотри свои же прошлые
выводы — адрес отправителя и есть их хот. Обычно их несколько, добавляй все.

API: Etherscan V2 (один ключ на 60+ EVM-сетей через chainid) и TronGrid.

Запуск: раз в 15–60 минут.
"""

import json
import sys
import time
import urllib.parse

from common import (
    cfg, esc, http, load_state, norm, now_iso, raise_wave, save_state,
    tainted_set, tg, tg_lines,
)

ETHERSCAN = "https://api.etherscan.io/v2/api"
TRONGRID = "https://api.trongrid.io"
STATE = "exposure_cursor.json"

# Известные миксеры/анонимайзеры — прямой контакт с ними красит сильнее санкций.
DEFAULT_MIXERS = {
    "0x8589427373d6d84e98730d7795d8f6f8731fda16",  # Tornado router
    "0x722122df12d4e14e13ac3b6895a86e84145b6967",
    "0xd90e2f925da726b50c4ed8d0fb90ad053324f31b",
    "0x910cbd523d972eb0a6f4cae4618ad62622b39dbf",
    "0xa160cdab225685da1d56aa342ad8841c3b53f291",
}


# ------------------------------------------------------------------ EVM


def evm_incoming(chainid, address, start_block, api_key, limit=200):
    """Нативные + токен-трансферы, только входящие."""
    out = []
    for action in ("txlist", "tokentx"):
        q = urllib.parse.urlencode(
            {
                "chainid": chainid,
                "module": "account",
                "action": action,
                "address": address,
                "startblock": start_block,
                "endblock": 99999999,
                "sort": "desc",
                "page": 1,
                "offset": limit,
                "apikey": api_key,
            }
        )
        try:
            body, _, _ = http(f"{ETHERSCAN}?{q}", timeout=60, retries=2)
            data = json.loads(body)
        except Exception as e:
            print(f"[evm {chainid}] {e}")
            continue

        if data.get("status") != "1" or not isinstance(data.get("result"), list):
            msg = str(data.get("result", ""))[:120]
            if "No transactions" not in msg and msg:
                print(f"[evm {chainid}] {msg}")
            time.sleep(0.25)
            continue

        for t in data["result"]:
            if norm(t.get("to", "")) != norm(address):
                continue
            out.append(
                {
                    "from": norm(t.get("from", "")),
                    "hash": t.get("hash"),
                    "block": int(t.get("blockNumber", 0)),
                    "ts": int(t.get("timeStamp", 0)),
                    "token": t.get("tokenSymbol", "native"),
                    "value": t.get("value", "0"),
                    "decimals": int(t.get("tokenDecimal") or 18),
                }
            )
        time.sleep(0.25)  # 5 rps на бесплатном ключе
    return out


# ------------------------------------------------------------------ TRON


def tron_incoming(address, since_ms, api_key=None):
    out = []
    headers = {"TRON-PRO-API-KEY": api_key} if api_key else {}
    endpoints = [
        f"{TRONGRID}/v1/accounts/{address}/transactions?limit=200&only_to=true"
        f"&min_timestamp={since_ms}",
        f"{TRONGRID}/v1/accounts/{address}/transactions/trc20?limit=200&only_to=true"
        f"&min_timestamp={since_ms}",
    ]
    for url in endpoints:
        try:
            body, _, _ = http(url, headers=headers, timeout=60, retries=2)
            data = json.loads(body)
        except Exception as e:
            print(f"[tron] {e}")
            continue

        for t in data.get("data", []):
            sender = t.get("from")
            if not sender:  # нативная транза лежит глубже
                try:
                    c = t["raw_data"]["contract"][0]["parameter"]["value"]
                    sender = c.get("owner_address")
                except Exception:
                    continue
            out.append(
                {
                    "from": sender,
                    "hash": t.get("transaction_id") or t.get("txID"),
                    "ts": int(t.get("block_timestamp", 0)) // 1000,
                    "token": (t.get("token_info") or {}).get("symbol", "TRX"),
                    "value": t.get("value", "0"),
                    "decimals": int((t.get("token_info") or {}).get("decimals") or 6),
                }
            )
        time.sleep(0.3)
    return out


# ------------------------------------------------------------------ логика


def check_target(target, bad, cursor):
    """target — запись из config (обменник или свой кошелёк). -> список попаданий"""
    addr = target["address"]
    ck = f"{target.get('chain')}:{norm(addr)}"
    hits = []

    if target.get("chain") == "trx":
        since = cursor.get(ck, {}).get("ts", int(time.time()) - 7 * 86400)
        txs = tron_incoming(addr, since * 1000, cfg().get("trongrid_api_key"))
    else:
        start = cursor.get(ck, {}).get("block", 0)
        txs = evm_incoming(
            target.get("chainid", 1), addr, start, cfg().get("etherscan_api_key", "")
        )

    for t in txs:
        if t["from"] in bad:
            hits.append({**t, "target": target})

    if txs:
        cursor[ck] = {
            "block": max(t.get("block", 0) for t in txs) + 1,
            "ts": max(t.get("ts", 0) for t in txs),
            "checked": now_iso(),
        }
    return hits


def fmt(hit, kind):
    t, tgt = hit, hit["target"]
    try:
        amount = int(t["value"]) / (10 ** t["decimals"])
        amount_s = f"{amount:,.4f} {t['token']}"
    except Exception:
        amount_s = f"{t['value']} {t['token']}"
    icon = "🛑" if kind == "own" else "⚠️"
    return [
        f"{icon} <b>{esc(tgt.get('name') or tgt.get('label') or tgt['address'][:12])}</b>"
        f"  ({esc(tgt.get('chain', '?'))})",
        f"   от <code>{esc(t['from'])}</code>",
        f"   {amount_s}   tx <code>{esc(str(t['hash'])[:24])}</code>",
    ]


def main():
    c = cfg()
    bad = tainted_set() | {norm(a) for a in DEFAULT_MIXERS}
    if len(bad) < 100:
        tg(
            "⚠️ <b>exposure_watch</b>: грязное множество почти пустое "
            f"({len(bad)}). Сначала прогони ofac_radar.py и opensanctions_radar.py."
        )
        return 1

    cursor = load_state(STATE)
    exch_hits, own_hits = [], []

    for tgt in c.get("watch_exchangers", []):
        exch_hits += check_target(tgt, bad, cursor)
    for tgt in c.get("my_wallets", []):
        own_hits += check_target(tgt, bad, cursor)

    save_state(STATE, cursor)

    lines = []
    if own_hits:
        lines.append(f"🛑 <b>ПРЯМАЯ ЭКСПОЗИЦИЯ НА ТВОЁМ КОШЕЛЬКЕ ({len(own_hits)})</b>")
        lines.append(
            "<i>Средства не двигать. Не отправлять на биржу. "
            "Зафиксировать tx, поднять историю происхождения.</i>"
        )
        lines.append("")
        for h in own_hits[:20]:
            lines += fmt(h, "own")
        raise_wave("direct_exposure", f"{len(own_hits)} входящих с грязных адресов",
                   len(own_hits))

    if exch_hits:
        lines.append("")
        lines.append(f"⚠️ <b>Обменники ловят грязь ({len(exch_hits)})</b>")
        lines.append("<i>Их хот тухнет. Выводы через них ближайшие дни — не делать.</i>")
        lines.append("")
        for h in exch_hits[:25]:
            lines += fmt(h, "exch")
        raise_wave("exchanger_taint", f"{len(exch_hits)} грязных входящих у обменников",
                   len(exch_hits))

    if lines:
        tg_lines(lines)
    else:
        print(f"[{now_iso()}] чисто. Грязных адресов в базе: {len(bad)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
