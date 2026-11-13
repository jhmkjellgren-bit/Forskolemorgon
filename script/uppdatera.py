"""Förskolemorgon: hämtar väder, Unikum-mejl och matsedel och skriver en krypterad docs/data.json.

Körs av GitHub Actions. Hemligheter läses från miljövariabler:
  GMAIL_ADRESS, GMAIL_APPLOSENORD  – det separata Gmail-kontot (applösenord)
  SIDANS_LOSENORD                  – lösenordet du skriver in på morgonskärmen
  BARN                             – t.ex. "Ella=Tryggheten Rosa;Nils=Viljan Gul"
  ANTHROPIC_API_KEY  (valfri)      – AI-sammanfattning av Unikum-inlägg
  SKOLMATEN_TOKEN    (valfri)      – client token från skolmaten.se
  SKOLMATEN_SKOLA    (valfri)      – t.ex. "forskolan-klavavagen"
"""
import base64
import datetime as dt
import email
import email.policy
import html
import imaplib
import json
import os
import re
import secrets
import sys
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ROT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("Europe/Stockholm")
PBKDF2_ITER = 250_000
VECKODAGAR = ["måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"]
MANADER = ["januari", "februari", "mars", "april", "maj", "juni", "juli",
           "augusti", "september", "oktober", "november", "december"]


def logg(*a):
    print(*a, file=sys.stderr)


# ---------- Vilken dag gäller skärmen? ----------
def maldag(nu):
    """Före kl 12 en vardag: idag. Annars nästa vardag."""
    d = nu.date()
    if nu.weekday() < 5 and nu.hour < 12:
        return d, "Idag"
    d += dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d, ("Imorgon" if d == nu.date() + dt.timedelta(days=1) else VECKODAGAR[d.weekday()].capitalize())


def dagtext(d, etikett):
    return f"{etikett} · {VECKODAGAR[d.weekday()]} {d.day} {MANADER[d.month - 1]}"


# ---------- Väder från SMHI ----------
def hamta_json(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "forskolemorgon/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def tolka_smhi(svar):
    """Returnerar lista av (utc-tid, {t, p, w, s}). Klarar både snow1g- och pmp3g-format."""
    ut = []
    for rad in svar.get("timeSeries", []):
        tid = rad.get("time") or rad.get("validTime")
        if not tid:
            continue
        t = dt.datetime.fromisoformat(tid.replace("Z", "+00:00"))
        if isinstance(rad.get("data"), dict):  # snow1g
            d = rad["data"]
            v = {"t": d.get("air_temperature"), "p": d.get("precipitation_amount_mean", 0),
                 "w": d.get("wind_speed"), "s": d.get("symbol_code")}
        else:  # pmp3g
            p = {x["name"]: x["values"][0] for x in rad.get("parameters", [])}
            v = {"t": p.get("t"), "p": p.get("pmean", 0), "w": p.get("ws"), "s": p.get("Wsymb2")}
        if v["t"] is None:
            continue
        ut.append((t, v))
    return ut


def hamta_vader(lat, lon, dag):
    urls = [
        f"https://opendata-download-metfcst.smhi.se/api/category/snow1g/version/1/geotype/point/lon/{lon}/lat/{lat}/data.json",
        f"https://opendata-download-metfcst.smhi.se/api/category/pmp3g/version/2/geotype/point/lon/{lon}/lat/{lat}/data.json",
    ]
    for url in urls:
        try:
            serie = tolka_smhi(hamta_json(url))
            if serie:
                break
        except Exception as e:  # noqa: BLE001
            logg("SMHI misslyckades:", url.split("/category/")[1].split("/")[0], e)
    else:
        return None
    timmar = []
    for t, v in serie:
        lok = t.astimezone(TZ)
        if lok.date() == dag and 6 <= lok.hour <= 19:
            timmar.append({"h": lok.hour, "t": round(float(v["t"]), 1), "p": round(float(v["p"] or 0), 1),
                           "w": round(float(v["w"] or 0), 1), "s": int(v["s"] or 0)})
    return timmar or None


# ---------- Unikum-mejl från Gmail ----------
def text_ur_mejl(msg):
    del_text = msg.get_body(preferencelist=("plain",))
    if del_text is not None:
        return del_text.get_content()
    del_html = msg.get_body(preferencelist=("html",))
    if del_html is None:
        return ""
    h = del_html.get_content()
    h = re.sub(r"(?is)<(script|style).*?</\1>", "", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>", "\n", h)
    return html.unescape(re.sub(r"<[^>]+>", "", h))


def tolka_unikum(rubrik, text):
    """Plockar ut grupprad och själva inlägget ur ett Unikum-mejl."""
    rader = [r.strip() for r in text.replace("\r", "").split("\n")]
    rader = [r for r in rader if r and r != "[bild]" and not r.startswith("[image")]
    # Vidarebefordrade mejl kan ha en rubrikrad överst; hoppa till efter rubriken om den finns.
    grupper, start = "", 0
    for i, r in enumerate(rader):
        if r.startswith("Från ") and " till " in r:
            grupper, start = r, i + 1
            break
    kropp = []
    for r in rader[start:]:
        if r.startswith("Ser inlägget konstigt ut") or r.startswith("Detta mail skickades"):
            break
        kropp.append(r)
    if kropp and kropp[0] == rubrik:
        kropp = kropp[1:]
    return grupper, "\n".join(kropp).strip()


def barn_for(grupper, barn):
    if not grupper:
        return [b["namn"] for b in barn]
    traffar = [b["namn"] for b in barn if b["grupp"].lower() in grupper.lower()]
    return traffar


def hamta_unikum(adress, losen, barn, dagar=7):
    m = imaplib.IMAP4_SSL("imap.gmail.com")
    m.login(adress, losen)
    m.select('"[Gmail]/All Mail"', readonly=True) if _har_allmail(m) else m.select("INBOX", readonly=True)
    typ, data = m.search(None, "X-GM-RAW", f'"from:unikum.net newer_than:{dagar}d"')
    ids = data[0].split() if typ == "OK" and data and data[0] else []
    ut = []
    for i in ids[-30:]:
        typ, d = m.fetch(i, "(RFC822)")
        if typ != "OK":
            continue
        msg = email.message_from_bytes(d[0][1], policy=email.policy.default)
        rubrik = str(msg.get("Subject", "")).strip()
        rubrik = re.sub(r"^(fwd?|vb|fw):\s*", "", rubrik, flags=re.I)
        try:
            datum = parsedate_to_datetime(msg.get("Date")).astimezone(TZ)
        except Exception:  # noqa: BLE001
            datum = dt.datetime.now(TZ)
        grupper, kropp = tolka_unikum(rubrik, text_ur_mejl(msg))
        vem = barn_for(grupper, barn)
        if not vem:
            continue  # gäller ingen av barnens grupper
        ut.append({"id": msg.get("Message-ID", str(i)), "rubrik": rubrik, "datum": datum.isoformat(),
                   "grupper": grupper, "barn": vem, "text": kropp[:3000]})
    m.logout()
    ut.sort(key=lambda x: x["datum"], reverse=True)
    return ut


def _har_allmail(m):
    try:
        typ, lst = m.list()
        return typ == "OK" and any(b"All Mail" in x or b"Alla mejl" in x for x in lst)
    except Exception:  # noqa: BLE001
        return False


# ---------- AI-sammanfattning ----------
AI_INSTRUKTION = """Du hjälper en förälder inför förskolelämning. Nedan finns inlägg från förskolans Unikum (senaste veckan) och dagens datum.
Svara ENDAST med JSON: {"inlagg":[{"id":"...","typ":"gor"|"info","kort":"...","galler_datum":"YYYY-MM-DD"|null}]}
- "gor": föräldern behöver göra, ta med eller tänka på något (ombyte, matsäck, stängt, utflykt, fotografering, ledig dag, blanketter).
- "info": bra att veta men ingen åtgärd.
- "kort": max 110 tecken på svenska, konkret, utan hälsningsfraser. Börja med det viktigaste.
- "galler_datum": det datum inlägget handlar om om ett sådant nämns, annars null. Tolka "på fredag" utifrån inläggets datum.
Ta med alla inlägg."""


def ai_sammanfatta(inlagg, dag, nyckel, modell):
    data = {"dagens_datum": dag.isoformat(),
            "inlagg": [{"id": x["id"], "skickat": x["datum"][:10], "rubrik": x["rubrik"], "text": x["text"]} for x in inlagg]}
    kropp = json.dumps({"model": modell, "max_tokens": 1500,
                        "messages": [{"role": "user", "content": AI_INSTRUKTION + "\n\n" + json.dumps(data, ensure_ascii=False)}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=kropp, method="POST", headers={
        "x-api-key": nyckel, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        svar = json.loads(r.read().decode())
    text = "".join(b.get("text", "") for b in svar.get("content", []))
    text = text[text.find("{"): text.rfind("}") + 1]
    return {x["id"]: x for x in json.loads(text).get("inlagg", [])}


def valj_inlagg(inlagg, ai, dag):
    """Behåll det som är relevant för måldagen."""
    ut = []
    for x in inlagg:
        a = ai.get(x["id"], {}) if ai else {}
        typ = a.get("typ", "info")
        kort = a.get("kort") or (x["text"].split("\n")[0][:110] if x["text"] else x["rubrik"])
        galler = a.get("galler_datum")
        skickat = dt.date.fromisoformat(x["datum"][:10])
        if galler:
            try:
                g = dt.date.fromisoformat(galler)
                if g < dag:
                    continue
            except ValueError:
                galler = None
        if not galler and (dag - skickat).days > (5 if typ == "gor" else 2):
            continue
        ut.append({"rubrik": x["rubrik"], "kort": kort, "typ": typ, "barn": x["barn"],
                   "galler_datum": galler, "skickat": x["datum"][:10]})
    ut.sort(key=lambda x: (x["typ"] != "gor", x["galler_datum"] or "9999"))
    return ut[:8]


# ---------- Matsedel från skolmaten.se (kräver egen client token) ----------
def hamta_matsedel(token, skola, dag):
    year, week, _ = dag.isocalendar()
    url = f"https://skolmaten.se/api/4/menu/school/{urllib.parse.quote(skola)}?year={year}&week={week}"
    svar = hamta_json(url, headers={"client-token": token, "Accept": "application/json"})
    dagar = (svar.get("WeekState") or {}).get("Days") or svar.get("Days") or []
    for d in dagar:
        if str(d.get("date", ""))[:10] == dag.isoformat():
            ratter = [m.get("name", "").strip() for m in d.get("Meals", []) if m.get("name")]
            return " / ".join(ratter) or None
    logg("Matsedel: hittade ingen dag som matchar", dag, "– fält i svaret:", list(svar)[:10])
    return None


# ---------- Kryptering (läses av docs/index.html med Web Crypto) ----------
def kryptera(obj, losen):
    salt, iv = secrets.token_bytes(16), secrets.token_bytes(12)
    nyckel = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITER).derive(losen.encode())
    ct = AESGCM(nyckel).encrypt(iv, json.dumps(obj, ensure_ascii=False).encode(), None)
    b = lambda x: base64.b64encode(x).decode()  # noqa: E731
    return {"v": 1, "iter": PBKDF2_ITER, "salt": b(salt), "iv": b(iv), "data": b(ct)}


def las_barn(s):
    barn = []
    for del_ in (s or "").split(";"):
        if "=" in del_:
            namn, grupp = del_.split("=", 1)
            barn.append({"namn": namn.strip(), "grupp": grupp.strip()})
    return barn


def main():
    inst = json.loads((ROT / "installningar.json").read_text(encoding="utf-8"))
    klader = json.loads((ROT / "klader.json").read_text(encoding="utf-8"))
    barn = las_barn(os.environ.get("BARN"))
    nu = dt.datetime.now(TZ)
    dag, etikett = maldag(nu)
    fel = []

    timmar = None
    try:
        timmar = hamta_vader(inst["plats"]["lat"], inst["plats"]["lon"], dag)
    except Exception as e:  # noqa: BLE001
        fel.append("vader"); logg("Väder:", e)

    unikum = []
    if os.environ.get("GMAIL_ADRESS") and os.environ.get("GMAIL_APPLOSENORD"):
        try:
            rå = hamta_unikum(os.environ["GMAIL_ADRESS"], os.environ["GMAIL_APPLOSENORD"].replace(" ", ""), barn)
            logg(f"Unikum: {len(rå)} inlägg för barnens grupper senaste veckan")
            ai = None
            if rå and os.environ.get("ANTHROPIC_API_KEY"):
                try:
                    ai = ai_sammanfatta(rå, dag, os.environ["ANTHROPIC_API_KEY"], inst.get("ai_modell", "claude-haiku-4-5"))
                except Exception as e:  # noqa: BLE001
                    fel.append("ai"); logg("AI:", e)
            unikum = valj_inlagg(rå, ai, dag)
        except Exception as e:  # noqa: BLE001
            fel.append("unikum"); logg("Unikum/Gmail:", e)

    matsedel = None
    if os.environ.get("SKOLMATEN_TOKEN") and os.environ.get("SKOLMATEN_SKOLA"):
        try:
            matsedel = hamta_matsedel(os.environ["SKOLMATEN_TOKEN"], os.environ["SKOLMATEN_SKOLA"], dag)
        except Exception as e:  # noqa: BLE001
            fel.append("matsedel"); logg("Matsedel:", e)

    innehall = {
        "dag": dagtext(dag, etikett), "datum": dag.isoformat(), "hamtad": nu.strftime("%-d/%-m %H:%M"),
        "barn": [b["namn"] for b in barn], "lamning": inst["lamning"], "hamtning": inst["hamtning"],
        "timmar": timmar, "klader": klader, "unikum": unikum, "matsedel": matsedel, "fel": fel,
    }
    losen = os.environ.get("SIDANS_LOSENORD")
    if not losen:
        sys.exit("SIDANS_LOSENORD saknas – lägg till den som hemlighet i GitHub.")
    (ROT / "docs" / "data.json").write_text(json.dumps(kryptera(innehall, losen)), encoding="utf-8")
    logg(f"Klart: {innehall['dag']}, {len(timmar or [])} väder-timmar, {len(unikum)} Unikum-rader, "
         f"matsedel {'ja' if matsedel else 'nej'}, fel: {fel or 'inga'}")


if __name__ == "__main__":
    main()
