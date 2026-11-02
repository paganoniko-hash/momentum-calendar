#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_calendar.py - aggiorna macro_calendar.json dal calendario ufficiale BLS (iCal, senza chiavi).

Cosa aggiorna da solo: Employment Situation (NFP), CPI, PPI, JOLTS.
Cosa resta a mano (nel file, con "src" diverso da "bls"): FOMC, PCE, GDP, Retail Sales.
Le righe manuali non vengono mai toccate.

Uso:  python3 update_calendar.py            # aggiorna
      python3 update_calendar.py --dry-run  # mostra cosa cambierebbe, senza scrivere
Dal bot: market_momentum chiama refresh() una volta a settimana (se la fonte e' raggiungibile).
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import urllib.request
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.abspath(__file__))
FILE = os.path.join(BASE, "macro_calendar.json")
URL = "https://www.bls.gov/schedule/news_release/bls.ics"
NY = ZoneInfo("America/New_York")

# (testo cercato nel titolo BLS, titolo nel calendario, impatto)
WATCH = [
    ("employment situation", "Non-Farm Payrolls / Employment Situation", "High"),
    ("consumer price index", "CPI - inflazione", "High"),
    ("producer price index", "PPI - prezzi alla produzione", "Medium"),
    ("job openings and labor turnover", "JOLTS (Job Openings)", "Medium"),
]
# parole che indicano varianti da scartare (es. CPI regionali, indici di import/export)
SKIP = ("average price", "import", "export", "real earnings", "county", "metropolitan", "state")


def fetch(url=URL):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "replace")


def parse_ics(text):
    text = re.sub(r"\r?\n[ \t]", "", text)  # unfold delle righe lunghe
    out = []
    for blk in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, flags=re.S):
        summ = re.search(r"^SUMMARY[^:]*:(.*)$", blk, flags=re.M)
        st = re.search(r"^DTSTART([^:]*):(\d{8})(?:T(\d{6}))?(Z?)", blk, flags=re.M)
        if not (summ and st):
            continue
        title = summ.group(1).strip()
        low = title.lower()
        if any(s in low for s in SKIP):
            continue
        hit = next((w for w in WATCH if w[0] in low), None)
        if not hit:
            continue
        d, hhmmss, z = st.group(2), st.group(3) or "083000", st.group(4)
        t = dt.datetime.strptime(d + hhmmss, "%Y%m%d%H%M%S")
        t = t.replace(tzinfo=dt.timezone.utc if z else NY).astimezone(NY)
        mo = re.search(r"for ([A-Z][a-z]+)", title)  # "... for October 2026"
        mese = mo.group(1)[:3].lower() if mo else ""
        out.append({"date": t.isoformat(), "country": "USD", "impact": hit[2],
                    "title": f"{hit[1]} ({mese})" if mese else hit[1], "src": "bls"})
    return out


FF_URLS = ["https://nfs.faireconomy.media/ff_calendar_thisweek.json",
           "https://nfs.faireconomy.media/ff_calendar_nextweek.json"]


def parse_ff(feeds):
    """feeds: lista di liste di eventi ForexFactory. Tiene USD con impatto High/Medium."""
    out = []
    for feed in feeds:
        for e in feed:
            if e.get("country") == "USD" and e.get("impact") in ("High", "Medium"):
                dt.datetime.fromisoformat(e["date"])
                out.append({"date": e["date"], "country": "USD", "impact": e["impact"],
                            "title": e["title"], "forecast": e.get("forecast") or "",
                            "previous": e.get("previous") or "", "src": "ff"})
    return out


def merge_ff(old, new):
    """Per le settimane coperte dal feed, il feed e' la fonte: sostituisce qualunque riga nello stesso periodo."""
    lo, hi = min(e["date"][:10] for e in new), max(e["date"][:10] for e in new)
    keep = [e for e in old if not (lo <= e["date"][:10] <= hi)]
    return sorted(keep + new, key=lambda e: e["date"])


def refresh_ff(dry=False, feeds=None):
    if feeds is None:
        feeds, err = [], None
        for u in FF_URLS:
            try:
                feeds.append(json.loads(fetch(u)))
            except Exception as e:  # noqa: BLE001
                err = e
        if not feeds:
            raise RuntimeError(f"ForexFactory non raggiungibile: {err}")
    new = parse_ff(feeds)
    if not new:
        raise RuntimeError("feed ForexFactory senza eventi USD: file non modificato")
    old = json.load(open(FILE, encoding="utf-8")) if os.path.exists(FILE) else []
    merged = merge_ff(old, new)
    before = {(e["date"][:10], e["title"]) for e in old}
    after = {(e["date"][:10], e["title"]) for e in merged}
    if not dry:
        write(merged)
    return sorted(after - before), sorted(before - after), len(merged)


def write(data):
    if os.path.exists(FILE):
        shutil.copy(FILE, FILE + ".bak")
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, FILE)


def refresh_any(dry=False):
    """Prima ForexFactory (JSON, 2 settimane, tutti gli eventi USA), poi BLS come ripiego."""
    try:
        return refresh_ff(dry=dry)
    except Exception as e1:  # noqa: BLE001
        try:
            return refresh(dry=dry)
        except Exception as e2:  # noqa: BLE001
            raise RuntimeError(f"{e1} | BLS: {e2}")


def merge(old, new, today):
    """Tiene le righe manuali e quelle passate; sostituisce le righe bls future con le nuove."""
    managed = ("Non-Farm", "CPI", "PPI", "JOLTS")  # le righe di questi 4 tipi le gestisce la fonte BLS
    keep = [e for e in old if not (e.get("src") == "bls" or e["title"].startswith(managed))
            or e["date"][:10] < today]
    manual_days = {(e["date"][:10], e["title"].split()[0].lower()) for e in keep}
    add = [e for e in new if e["date"][:10] >= today
           and (e["date"][:10], e["title"].split()[0].lower()) not in manual_days]
    return sorted(keep + add, key=lambda e: e["date"])


def refresh(dry=False, text=None):
    today = dt.date.today().isoformat()
    new = parse_ics(text if text is not None else fetch())
    if not new:
        raise RuntimeError("nessun evento riconosciuto nel calendario BLS: file non modificato")
    old = json.load(open(FILE, encoding="utf-8")) if os.path.exists(FILE) else []
    merged = merge(old, new, today)
    before = {(e["date"][:10], e["title"]) for e in old}
    after = {(e["date"][:10], e["title"]) for e in merged}
    if not dry:
        if os.path.exists(FILE):
            shutil.copy(FILE, FILE + ".bak")
        tmp = FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=1)
        os.replace(tmp, FILE)
    return sorted(after - before), sorted(before - after), len(merged)


def pull(url, dry=False):
    """Scarica macro_calendar.json gia' pronto (es. da GitHub raw) e lo sostituisce dopo i controlli."""
    data = json.loads(fetch(url))
    if not isinstance(data, list) or not data or not all("date" in e and "title" in e for e in data):
        raise RuntimeError("file scaricato non valido: calendario non modificato")
    for e in data:
        dt.datetime.fromisoformat(e["date"])  # solleva errore se una data e' malformata
    if not dry:
        if os.path.exists(FILE):
            shutil.copy(FILE, FILE + ".bak")
        tmp = FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, FILE)
    return len(data)


def sync():
    """Se CALENDAR_URL e' impostata scarica il file da li', altrimenti prova la fonte BLS diretta."""
    url = os.environ.get("CALENDAR_URL")
    return pull(url) if url else refresh_any()[2]


def due(days=7):
    """True se il file non e' stato modificato da piu' di 'days' giorni."""
    return (not os.path.exists(FILE)) or (dt.datetime.now().timestamp() - os.path.getmtime(FILE) > days * 86400)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    try:
        if os.environ.get("CALENDAR_URL"):
            print(f"Calendario scaricato: {pull(os.environ['CALENDAR_URL'], dry=a.dry_run)} eventi")
            raise SystemExit(0)
        added, removed, n = refresh_any(dry=a.dry_run)
    except Exception as e:  # noqa: BLE001
        print(f"Aggiornamento non riuscito: {e}")
        raise SystemExit(1)
    print(f"{'(prova) ' if a.dry_run else ''}Eventi totali: {n}")
    for x in added:
        print("  + ", *x)
    for x in removed:
        print("  - ", *x)
