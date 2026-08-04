#!/usr/bin/env python3
# ————————————————————————————————————————————————————————————————
# WELLENWERK ALERT — serverseitige Wache für die BTC-Marktstruktur.
# Läuft per GitHub Actions (stündlich), rechnet dieselbe Logik wie
# die Wellenwerk-App und meldet nur ZUSTANDSWECHSEL per Telegram:
#   · Haltung gewechselt (konstruktiv / neutral / defensiv)
#   · Regime gekippt (Kurs über/unter SMA 200)
#   · RSI-Extremzone betreten (<30 / >70)
#   · 0,618-Retracement verloren oder zurückerobert
#   · Zielzone 1,272 erreicht (einmal je Swing)
#   · Neuer Swing-Pivot bestätigt (neue Fib-Level)
# Keine Prognosen. Keine Dauerbeschallung. Nur Änderungen.
#
# Benötigte Secrets im Repo: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
# Ohne Secrets läuft das Skript im Trockenmodus (druckt statt sendet).
# Selbsttest ohne Netz:  python3 wellenwerk_alert.py --selftest
# ————————————————————————————————————————————————————————————————
import json
import os
import sys
import random
import urllib.request
import urllib.parse
from datetime import datetime, timezone

STATE_FILE = "state.json"
ZZ_THR = 0.08          # Swing-Schwelle wie in der App (8 %)
ATR_MULT = 2           # Referenz für den ATR-Stop in Meldungen
UA = {"User-Agent": "wellenwerk-alert/1.0"}


# —— Hilfen ————————————————————————————————————————————————————

def fmt(v, dec=0):
    """Deutsche Zahlformatierung: 12.345,67"""
    if v is None:
        return "–"
    s = f"{v:,.{dec}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def http_json(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# —— Datenquellen (Binance → Kraken → CoinGecko) ————————————————

def load_binance():
    for host in ("https://data-api.binance.vision", "https://api.binance.com"):
        try:
            k = http_json(f"{host}/api/v3/klines?symbol=BTCEUR&interval=1d&limit=1000")
            rows = [{"t": x[0], "o": float(x[1]), "h": float(x[2]),
                     "l": float(x[3]), "c": float(x[4]), "v": float(x[5])} for x in k]
            if len(rows) >= 250:
                return rows, f"Binance ({host.split('//')[1]})"
        except Exception:
            continue
    raise RuntimeError("Binance nicht erreichbar")


def load_kraken():
    j = http_json("https://api.kraken.com/0/public/OHLC?pair=XBTEUR&interval=1440")
    res = j["result"]
    key = next(k for k in res if k != "last")
    rows = [{"t": int(x[0]) * 1000, "o": float(x[1]), "h": float(x[2]),
             "l": float(x[3]), "c": float(x[4]), "v": float(x[6])} for x in res[key]]
    if len(rows) < 250:
        raise RuntimeError("Kraken: zu wenig Historie")
    return rows, "Kraken"


def load_coingecko():
    j = http_json("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
                  "?vs_currency=eur&days=365&interval=daily")
    vols = j.get("total_volumes", [])
    rows = []
    for i, p in enumerate(j["prices"]):
        v = vols[i][1] if i < len(vols) else 0.0
        rows.append({"t": p[0], "o": p[1], "h": p[1], "l": p[1], "c": p[1], "v": v})
    if len(rows) < 250:
        raise RuntimeError("CoinGecko: zu wenig Historie")
    return rows, "CoinGecko (Fallback, nur Schlusskurse)"


def load_data():
    for fn in (load_binance, load_kraken, load_coingecko):
        try:
            return fn()
        except Exception:
            continue
    raise RuntimeError("Keine Datenquelle erreichbar")


# —— Indikatoren (identisch zur App) ————————————————————————————

def sma(vals, n):
    out = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(vals, n):
    k = 2 / (n + 1)
    out = [None] * len(vals)
    prev = None
    seed = 0.0
    for i, v in enumerate(vals):
        if i < n:
            seed += v
            if i == n - 1:
                prev = seed / n
                out[i] = prev
            continue
        prev = v * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi_wilder(vals, n=14):
    out = [None] * len(vals)
    if len(vals) <= n:
        return out
    g = l = 0.0
    for i in range(1, n + 1):
        d = vals[i] - vals[i - 1]
        if d > 0:
            g += d
        else:
            l -= d
    g /= n
    l /= n
    out[n] = 100 - 100 / (1 + (1e9 if l == 0 else g / l))
    for i in range(n + 1, len(vals)):
        d = vals[i] - vals[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
        out[i] = 100 - 100 / (1 + (1e9 if l == 0 else g / l))
    return out


def atr_wilder(rows, n=14):
    out = [None] * len(rows)
    if len(rows) <= n:
        return out
    tr = []
    for i, r in enumerate(rows):
        if i == 0:
            tr.append(r["h"] - r["l"])
        else:
            pc = rows[i - 1]["c"]
            tr.append(max(r["h"] - r["l"], abs(r["h"] - pc), abs(r["l"] - pc)))
    a = sum(tr[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(rows)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def zigzag(closes, thr):
    piv = []
    direction = 0
    ext_p, ext_i = closes[0], 0
    for i in range(1, len(closes)):
        c = closes[i]
        if direction >= 0:
            if c > ext_p:
                ext_p, ext_i = c, i
            elif c <= ext_p * (1 - thr):
                piv.append({"i": ext_i, "p": ext_p, "hi": True})
                direction, ext_p, ext_i = -1, c, i
                continue
        if direction < 0:
            if c < ext_p:
                ext_p, ext_i = c, i
            elif c >= ext_p * (1 + thr):
                piv.append({"i": ext_i, "p": ext_p, "hi": False})
                direction, ext_p, ext_i = 1, c, i
    return piv  # nur bestätigte Pivots


# —— Analyse ————————————————————————————————————————————————————

def analyse(rows):
    closes = [r["c"] for r in rows]
    n = len(closes)
    last = n - 1
    price = closes[last]

    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    s200 = sma(closes, 200)
    r14 = rsi_wilder(closes, 14)
    atr = atr_wilder(rows, 14)

    mom30 = (price / closes[last - 30] - 1) * 100 if n > 30 else 0.0

    vols = [r["v"] for r in rows]
    vol_trend = None
    if n > 60:
        v1 = sum(vols[-30:]) / 30
        v0 = sum(vols[-60:-30]) / 30
        if v0 > 0:
            vol_trend = (v1 / v0 - 1) * 100

    piv = zigzag(closes, ZZ_THR)
    swing = None
    if len(piv) >= 2:
        a, b = piv[-2], piv[-1]
        up = b["p"] > a["p"]
        swing = {
            "id": f"{a['i']}-{b['i']}",
            "up": up,
            "a": a["p"], "b": b["p"],
            "retr618": b["p"] - (b["p"] - a["p"]) * 0.618,
            "ext1272": a["p"] + (b["p"] - a["p"]) * 1.272,
        }

    s_trend = 0
    if e20[last] is not None and e50[last] is not None:
        s_trend = 1 if e20[last] > e50[last] else -1
    s_rsi = 0
    if r14[last] is not None:
        s_rsi = 1 if r14[last] < 30 else (-1 if r14[last] > 70 else 0)
    s_mom = 1 if mom30 > 5 else (-1 if mom30 < -5 else 0)
    s_fib = 0
    if swing:
        if swing["up"]:
            s_fib = 1 if price >= swing["retr618"] else -1
        else:
            s_fib = -1 if price <= swing["retr618"] else 1
    s_vol = s_trend if (vol_trend is not None and vol_trend > 10) else 0

    konfluenz = s_trend + s_rsi + s_mom + s_fib + s_vol
    regime = "über" if (s200[last] is not None and price > s200[last]) else "unter"

    if konfluenz >= 2 and regime == "über":
        stance = "konstruktiv"
    elif konfluenz <= -2 or regime == "unter":
        stance = "defensiv"
    else:
        stance = "neutral"

    rsi_zone = "mitte"
    if r14[last] is not None:
        rsi_zone = "unter30" if r14[last] < 30 else ("ueber70" if r14[last] > 70 else "mitte")

    target_hit = False
    if swing:
        target_hit = price >= swing["ext1272"] if swing["up"] else price <= swing["ext1272"]

    return {
        "price": price,
        "rsi": r14[last],
        "atr": atr[last],
        "s200": s200[last],
        "mom30": mom30,
        "signals": {"trend": s_trend, "rsi": s_rsi, "mom": s_mom, "fib": s_fib, "vol": s_vol},
        "konfluenz": konfluenz,
        "regime": regime,
        "stance": stance,
        "rsi_zone": rsi_zone,
        "swing": swing,
        "pivots": len(piv),
        "target_hit": target_hit,
    }


# —— Meldungen ——————————————————————————————————————————————————

def pfeile(sig):
    sym = {1: "↑", -1: "↓", 0: "·"}
    return (f"Trend{sym[sig['trend']]} RSI{sym[sig['rsi']]} "
            f"Mom{sym[sig['mom']]} Fib{sym[sig['fib']]} Vol{sym[sig['vol']]}")


def stop_zeile(a):
    if a["atr"] is None:
        return ""
    return f"\nATR-Stop-Referenz: {fmt(a['price'] - ATR_MULT * a['atr'])} € (Kurs − {ATR_MULT}×ATR)"


def snapshot(a, quelle):
    z = [f"Kurs {fmt(a['price'])} € · {'%+.1f' % a['mom30']} % / 30 T",
         f"Haltung: {a['stance'].upper()} · Konfluenz {a['konfluenz']:+d} ({pfeile(a['signals'])})",
         f"Regime: {a['regime']} SMA 200 ({fmt(a['s200'])} €)"]
    if a["swing"]:
        sw = a["swing"]
        z.append(f"Swing {'auf' if sw['up'] else 'ab'}wärts {fmt(sw['a'])} → {fmt(sw['b'])} €")
        z.append(f"0,618 bei {fmt(sw['retr618'])} € · Ziel 1,272 bei {fmt(sw['ext1272'])} €")
    z.append(f"Quelle: {quelle}")
    return "\n".join(z) + stop_zeile(a)


def vergleiche(prev, cur):
    """Nur Zustandswechsel melden — die Kernidee des Wächters."""
    msgs = []
    if prev.get("stance") != cur["stance"]:
        msgs.append(f"⚙ Haltung gewechselt: {prev.get('stance', '?').upper()} → {cur['stance'].upper()}\n"
                    f"Konfluenz {cur['konfluenz']:+d} ({pfeile(cur['signals'])}), "
                    f"Kurs {cur['regime']} SMA 200.{stop_zeile(cur)}")
    if prev.get("regime") != cur["regime"]:
        msgs.append(f"⚙ Regime gekippt: Kurs jetzt {cur['regime'].upper()} der SMA 200 "
                    f"({fmt(cur['s200'])} €).")
    if cur["rsi_zone"] != "mitte" and prev.get("rsi_zone") != cur["rsi_zone"]:
        lage = "überverkauft (<30)" if cur["rsi_zone"] == "unter30" else "überkauft (>70)"
        msgs.append(f"⚙ RSI 14 jetzt {lage}: {cur['rsi']:.1f}")

    sw, psw = cur.get("swing"), prev.get("swing")
    gleicher_swing = sw and psw and sw["id"] == psw["id"]

    if cur["pivots"] > prev.get("pivots", 0) and sw:
        msgs.append(f"⚙ Neuer Swing bestätigt ({'auf' if sw['up'] else 'ab'}wärts "
                    f"{fmt(sw['a'])} → {fmt(sw['b'])} €).\n"
                    f"Neue Level: 0,618 bei {fmt(sw['retr618'])} € · Ziel 1,272 bei {fmt(sw['ext1272'])} €")
    elif gleicher_swing:
        pfib = prev.get("signals", {}).get("fib", 0)
        if cur["signals"]["fib"] != pfib and cur["signals"]["fib"] != 0:
            verloren = cur["signals"]["fib"] < 0
            msgs.append(f"⚙ 0,618-Level ({fmt(sw['retr618'])} €) "
                        f"{'VERLOREN — Struktur beschädigt' if verloren else 'zurückerobert — Struktur wieder intakt'}."
                        f"{stop_zeile(cur)}")
        if cur["target_hit"] and not prev.get("target_hit", False):
            msgs.append(f"⚙ ZIELZONE erreicht: 1,272-Extension bei {fmt(sw['ext1272'])} €.\n"
                        f"Regel-Erinnerung: Teilverkauf erwägen, Stop der Restposition auf Einstand.")
    return msgs


# —— Telegram & Zustand —————————————————————————————————————————

def sende(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    kopf = "🔧 Wellenwerk · BTC\n" + datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC") + "\n\n"
    if not token or not chat:
        print("[TROCKENMODUS — keine Telegram-Secrets gesetzt]\n" + kopf + text)
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": kopf + text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                 data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        r.read()


def lade_zustand():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def speichere_zustand(a):
    zustand = {k: a[k] for k in ("stance", "regime", "rsi_zone", "pivots", "target_hit", "price")}
    zustand["swing"] = a["swing"]
    zustand["signals"] = a["signals"]
    zustand["ts"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(zustand, f, ensure_ascii=False, indent=1)


# —— Selbsttest ohne Netz ————————————————————————————————————————

def selftest():
    random.seed(42)
    rows, p = [], 30000.0
    for i in range(600):
        drift = 0.004 if (i // 120) % 2 == 0 else -0.003   # abwechselnde Phasen
        p *= 1 + drift + random.gauss(0, 0.02)
        h = p * (1 + abs(random.gauss(0, 0.01)))
        l = p * (1 - abs(random.gauss(0, 0.01)))
        rows.append({"t": i, "o": p, "h": h, "l": l, "c": p, "v": 1000 + random.random() * 500})
    a = analyse(rows)
    assert a["price"] > 0 and a["konfluenz"] is not None and a["pivots"] >= 2
    print("Analyse ok:", a["stance"], f"Konfluenz {a['konfluenz']:+d}",
          f"Regime {a['regime']}", f"Pivots {a['pivots']}")

    # Zustandswechsel künstlich erzwingen und Meldungserzeugung prüfen
    prev = {k: a[k] for k in ("stance", "regime", "rsi_zone", "pivots", "target_hit")}
    prev["swing"] = a["swing"]
    prev["signals"] = dict(a["signals"])
    prev["stance"] = "neutral" if a["stance"] != "neutral" else "defensiv"
    prev["regime"] = "unter" if a["regime"] == "über" else "über"
    prev["pivots"] = a["pivots"] - 1
    msgs = vergleiche(prev, a)
    assert len(msgs) >= 3, "zu wenige Meldungen erzeugt"
    print(f"Meldungslogik ok ({len(msgs)} Meldungen):\n")
    sende("\n\n".join(msgs))
    print("\nSelbsttest bestanden.")


# —— Hauptlauf ——————————————————————————————————————————————————

def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    rows, quelle = load_data()
    a = analyse(rows)
    prev = lade_zustand()
    if prev is None:
        sende("Wächter aktiv — Startbericht:\n\n" + snapshot(a, quelle))
    else:
        msgs = vergleiche(prev, a)
        if msgs:
            sende("\n\n".join(msgs))
        else:
            print("Kein Zustandswechsel — keine Meldung.")
    speichere_zustand(a)


if __name__ == "__main__":
    main()
