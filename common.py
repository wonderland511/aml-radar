#!/usr/bin/env python3
"""
common.py — общий слой для всех радаров.
Только stdlib, никаких зависимостей.
"""

import os
import re
import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("RADAR_STATE_DIR", os.path.join(ROOT, "state"))
os.makedirs(STATE_DIR, exist_ok=True)

UA = "aml-radar/1.0 (self-hosted compliance monitor)"

_cfg_cache = None


def cfg():
    global _cfg_cache
    if _cfg_cache is None:
        path = os.environ.get("RADAR_CONFIG", os.path.join(ROOT, "config.json"))
        with open(path, encoding="utf-8") as f:
            _cfg_cache = json.load(f)
    return _cfg_cache


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ состояние


def spath(name):
    return os.path.join(STATE_DIR, name)


def load_state(name, default=None):
    try:
        with open(spath(name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {} if default is None else default


def save_state(name, data):
    """Атомарная запись: либо старый файл, либо новый, но не половина."""
    tmp = spath(name) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, spath(name))


# ------------------------------------------------------------------ HTTP


def http(url, headers=None, timeout=180, retries=3, want_gzip=True):
    """
    -> (body_bytes | None, headers_dict, status)
    Возвращает (None, hdrs, 304), если сервер сказал Not Modified.
    """
    hdrs = {"User-Agent": UA}
    if want_gzip:
        hdrs["Accept-Encoding"] = "gzip"
    hdrs.update(headers or {})

    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return body, dict(r.headers), r.status
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return None, dict(e.headers), 304
            last = e
            if e.code < 500 and e.code != 429:
                raise
        except Exception as e:
            last = e
        time.sleep(2 ** attempt)
    raise last


def http_cached(url, cache_key, timeout=180):
    """
    Условный GET по ETag / Last-Modified.
    Экономит трафик: OFAC-файл 28 МБ, качать его 24 раза в сутки незачем.
    -> (body_bytes, was_modified)
    """
    meta = load_state("http_meta.json")
    entry = meta.get(cache_key, {})
    cache_file = spath(f"cache_{cache_key}")

    headers = {}
    if entry.get("etag"):
        headers["If-None-Match"] = entry["etag"]
    if entry.get("last_modified"):
        headers["If-Modified-Since"] = entry["last_modified"]

    body, hdrs, status = http(url, headers, timeout=timeout)

    if status == 304 and os.path.exists(cache_file):
        with open(cache_file, "rb") as f:
            return f.read(), False

    if body is None:  # 304, но кэш потерян — качаем без условий
        body, hdrs, _ = http(url, timeout=timeout)

    with open(cache_file, "wb") as f:
        f.write(body)
    meta[cache_key] = {
        "etag": hdrs.get("ETag"),
        "last_modified": hdrs.get("Last-Modified"),
        "fetched": now_iso(),
    }
    save_state("http_meta.json", meta)
    return body, True


# ------------------------------------------------------------------ telegram


def _tg_creds():
    c = cfg().get("telegram", {})
    token = os.environ.get("TG_TOKEN") or c.get("token")
    chat = os.environ.get("TG_CHAT") or c.get("chat_id")
    if not token or not chat:
        raise RuntimeError("Нет TG_TOKEN / TG_CHAT (env или config.json)")
    return token, chat


def tg(text):
    token, chat = _tg_creds()
    data = urllib.parse.urlencode(
        {
            "chat_id": chat,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode()
    for attempt in range(3):
        try:
            urllib.request.urlopen(
                f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=30
            )
            return
        except Exception:
            time.sleep(2 ** attempt)
    print("[!] Telegram недоступен, сообщение потеряно:\n", text[:500])


def tg_lines(lines, limit=3500):
    """Режем по строкам, чтобы не порвать HTML-тег посередине."""
    buf = ""
    for line in lines:
        line = line if len(line) < limit else line[: limit - 3] + "..."
        if len(buf) + len(line) + 1 > limit:
            tg(buf)
            buf = ""
            time.sleep(0.4)
        buf += line + "\n"
    if buf.strip():
        tg(buf)


def esc(s):
    return (
        str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


# ------------------------------------------------------------------ адреса


EVM_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def norm(addr):
    """EVM-адреса регистронезависимы, всё остальное — нет."""
    addr = (addr or "").strip()
    return addr.lower() if EVM_RE.match(addr) else addr


def key(asset, addr):
    return f"{(asset or '?').upper()}:{norm(addr)}"


# ------------------------------------------------------------------ триггеры


def raise_wave(source, reason, count=0):
    """
    Любой радар, увидевший изменение, дёргает это.
    aml_journal.py читает файл и помечает все кошельки как «нужна перепроверка».
    """
    waves = load_state("waves.json", [])
    waves.append(
        {"ts": now_iso(), "source": source, "reason": reason, "count": count}
    )
    save_state("waves.json", waves[-500:])


def tainted_set():
    """
    Объединённое множество «грязных» адресов из всех источников.
    Используется exposure_watch.py.  -> set(норм. адресов)
    """
    out = set()
    for fname in ("ofac_addrs.json", "opensanctions_addrs.json"):
        for rec in load_state(fname, {}).values():
            out.add(norm(rec["addr"]))
    for a in cfg().get("extra_blacklist", []):
        out.add(norm(a))
    return out
