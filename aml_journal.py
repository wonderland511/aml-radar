#!/usr/bin/env python3
"""
aml_journal.py — слой 5. Журнал. То, что реально спасает при заморозке.

Скоринг РЕТРОАКТИВЕН: адрес, чистый сегодня, через месяц может стать грязным
за ту же самую старую транзакцию — вендор дообогатил кластер. Поэтому нужна
не разовая проверка, а история проверок с датами.

Когда биржа через полгода тормозит депозит, работает ровно одно: показать,
что на момент получения адрес был чистым, и предъявить происхождение.

Команды:
  python3 aml_journal.py init
  python3 aml_journal.py add-wallet --chain eth --address 0x.. --label "хот"
  python3 aml_journal.py add-receipt --address 0x.. --source "ObmenX" \
        --txid 0xabc --amount "1500 USDT" --note "обмен с карты"
  python3 aml_journal.py record --address 0x.. --vendor chainalysis \
        --score 3 --verdict clean --note "прямых нет, 2 хопа чисто"
  python3 aml_journal.py due          # что пора перепроверить
  python3 aml_journal.py report --address 0x..   # пакет доказательств
  python3 aml_journal.py waves        # хронология сработавших триггеров
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from common import ROOT, load_state, now_iso

DB = os.environ.get("RADAR_DB", os.path.join(ROOT, "state", "aml.db"))
RESCAN_DAYS = 7


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init():
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS wallets(
              id INTEGER PRIMARY KEY,
              chain TEXT NOT NULL,
              address TEXT NOT NULL UNIQUE,
              label TEXT,
              created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS receipts(
              id INTEGER PRIMARY KEY,
              wallet_id INTEGER NOT NULL REFERENCES wallets(id),
              source TEXT,        -- откуда пришло: обменник, биржа, контрагент
              txid TEXT,
              amount TEXT,
              received TEXT NOT NULL,
              note TEXT
            );
            CREATE TABLE IF NOT EXISTS scans(
              id INTEGER PRIMARY KEY,
              wallet_id INTEGER NOT NULL REFERENCES wallets(id),
              ts TEXT NOT NULL,
              vendor TEXT NOT NULL,   -- chainalysis / trm / crystal / amlbot ...
              score REAL,             -- 0..100 в шкале вендора
              verdict TEXT,           -- clean / low / medium / high
              note TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_scans_w ON scans(wallet_id, ts);
            """
        )
    print(f"OK: {DB}")


def wallet_id(c, address, create_chain=None, label=None):
    row = c.execute(
        "SELECT id FROM wallets WHERE lower(address)=lower(?)", (address,)
    ).fetchone()
    if row:
        return row["id"]
    if not create_chain:
        sys.exit(f"Кошелёк {address} не заведён. Сначала add-wallet.")
    cur = c.execute(
        "INSERT INTO wallets(chain,address,label,created) VALUES(?,?,?,?)",
        (create_chain, address, label, now_iso()),
    )
    return cur.lastrowid


def cmd_add_wallet(a):
    with conn() as c:
        wid = wallet_id(c, a.address, a.chain, a.label)
    print(f"wallet #{wid}: {a.address}")
    print("Сделай baseline-скан ДВУМЯ вендорами и запиши через `record`.")
    print("Один вендор — не показатель, Chainalysis и TRM регулярно расходятся.")


def cmd_add_receipt(a):
    with conn() as c:
        wid = wallet_id(c, a.address)
        c.execute(
            "INSERT INTO receipts(wallet_id,source,txid,amount,received,note)"
            " VALUES(?,?,?,?,?,?)",
            (wid, a.source, a.txid, a.amount, a.received or now_iso(), a.note),
        )
    print("записано")


def cmd_record(a):
    with conn() as c:
        wid = wallet_id(c, a.address)
        c.execute(
            "INSERT INTO scans(wallet_id,ts,vendor,score,verdict,note)"
            " VALUES(?,?,?,?,?,?)",
            (wid, now_iso(), a.vendor, a.score, a.verdict, a.note),
        )
        prev = c.execute(
            "SELECT score,verdict,ts FROM scans WHERE wallet_id=? AND vendor=?"
            " ORDER BY ts DESC LIMIT 2",
            (wid, a.vendor),
        ).fetchall()
    print("записано")
    if len(prev) == 2 and prev[1]["score"] is not None and a.score is not None:
        delta = a.score - prev[1]["score"]
        if delta > 0:
            print(
                f"⚠️  Скоринг вырос: {prev[1]['score']} → {a.score} (+{delta:.1f}) "
                f"с {prev[1]['ts']}. Ретроактивная переоценка — разбирайся, "
                f"какое старое поступление подтухло."
            )


def cmd_due(a):
    waves = load_state("waves.json", [])
    cutoff = datetime.now(timezone.utc) - timedelta(days=RESCAN_DAYS)
    recent = [
        w for w in waves
        if datetime.fromisoformat(w["ts"]) > datetime.now(timezone.utc) - timedelta(days=3)
    ]

    with conn() as c:
        rows = c.execute(
            """
            SELECT w.id, w.chain, w.address, w.label,
                   (SELECT max(ts) FROM scans s WHERE s.wallet_id=w.id) AS last_scan
            FROM wallets w ORDER BY last_scan IS NULL DESC, last_scan ASC
            """
        ).fetchall()

    if recent:
        print(f"🔔 За 3 дня сработало триггеров: {len(recent)}")
        for w in recent[-8:]:
            print(f"   [{w['ts']}] {w['source']}: {w['reason']}")
        print("   → перепроверять ВСЁ, не только просроченное.\n")

    due, ok = [], []
    for r in rows:
        stale = (not r["last_scan"]) or datetime.fromisoformat(r["last_scan"]) < cutoff
        (due if (stale or recent) else ok).append(r)

    print(f"К перепроверке: {len(due)} из {len(rows)}")
    for r in due:
        print(
            f"  {r['chain']:<5} {r['address']:<46} "
            f"{r['label'] or '':<14} последний скан: {r['last_scan'] or 'НИКОГДА'}"
        )
    if ok:
        print(f"\nСвежие ({len(ok)}): " + ", ".join(x["address"][:12] + "…" for x in ok))


def cmd_report(a):
    with conn() as c:
        w = c.execute(
            "SELECT * FROM wallets WHERE lower(address)=lower(?)", (a.address,)
        ).fetchone()
        if not w:
            sys.exit("нет такого кошелька")
        receipts = c.execute(
            "SELECT * FROM receipts WHERE wallet_id=? ORDER BY received", (w["id"],)
        ).fetchall()
        scans = c.execute(
            "SELECT * FROM scans WHERE wallet_id=? ORDER BY ts", (w["id"],)
        ).fetchall()

    doc = {
        "wallet": dict(w),
        "receipts": [dict(r) for r in receipts],
        "scans": [dict(s) for s in scans],
        "generated": now_iso(),
    }
    out = os.path.join(ROOT, f"proof_{w['address'][:10]}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    print(f"=== {w['address']} ({w['chain']}) — {w['label'] or ''}")
    print(f"Поступлений: {len(receipts)}, проверок: {len(scans)}")
    for s in scans:
        print(f"  {s['ts']}  {s['vendor']:<14} score={s['score']} {s['verdict']}")
    print(f"\nПакет: {out}")
    print("К нему приложи скриншоты отчётов вендоров и чеки обменников.")


def cmd_waves(a):
    for w in load_state("waves.json", [])[-40:]:
        print(f"{w['ts']}  {w['source']:<18} {w['reason']}")


def main():
    p = argparse.ArgumentParser(description="Журнал AML-проверок")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(fn=lambda a: init())

    q = sub.add_parser("add-wallet")
    q.add_argument("--chain", required=True)
    q.add_argument("--address", required=True)
    q.add_argument("--label", default="")
    q.set_defaults(fn=cmd_add_wallet)

    q = sub.add_parser("add-receipt")
    q.add_argument("--address", required=True)
    q.add_argument("--source", required=True)
    q.add_argument("--txid", default="")
    q.add_argument("--amount", default="")
    q.add_argument("--received", default="")
    q.add_argument("--note", default="")
    q.set_defaults(fn=cmd_add_receipt)

    q = sub.add_parser("record")
    q.add_argument("--address", required=True)
    q.add_argument("--vendor", required=True)
    q.add_argument("--score", type=float)
    q.add_argument("--verdict", default="")
    q.add_argument("--note", default="")
    q.set_defaults(fn=cmd_record)

    sub.add_parser("due").set_defaults(fn=cmd_due)

    q = sub.add_parser("report")
    q.add_argument("--address", required=True)
    q.set_defaults(fn=cmd_report)

    sub.add_parser("waves").set_defaults(fn=cmd_waves)

    a = p.parse_args()
    if a.cmd != "init" and not os.path.exists(DB):
        init()
    a.fn(a)


if __name__ == "__main__":
    main()
