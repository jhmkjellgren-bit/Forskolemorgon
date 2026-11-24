"""Förskolemorgon: hämtar väder, Unikum-mejl och matsedel och skriver en krypterad docs/data.json.

Körs av GitHub Actions. Hemligheter läses från miljövariabler:
  GMAIL_ADRESS, GMAIL_APPLOSENORD  – det separata Gmail-kontot (applösenord)
  SIDANS_LOSENORD                  – lösenordet du skriver in på morgonskärmen
  BARN                             – t.ex. "Ella=Tryggheten Rosa;Nils=Viljan Gul"
  ANTHROPIC_API_KEY  (valfri)      – AI-sammanfattning av Unikum-inlägg
  SKOLMATEN_TOKEN    (valfri)      – client token från skolmaten.se
  SKOLMATEN_SKOLA    (valfri)      – t.ex. "forskolan-klavavagen"
  NAMNSDAGAR         (valfri)      – namn att bevaka, t.ex. "Anna, Erik, Maja"
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
    return d, ("Imorgon" if d == nu.date() + dt.timedelta(days=1) else "")


def dagtext(d, etikett):
    dagnamn = f"{VECKODAGAR[d.weekday()]} {d.day} {MANADER[d.month - 1]}"
    return f"{etikett} · {dagnamn}" if etikett else dagnamn.capitalize()


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
                 "w": d.get("wind_speed"), "s": d.get("symbol_code"),
                 "d": d.get("wind_from_direction"), "g": d.get("wind_speed_of_gust")}
        else:  # pmp3g
            p = {x["name"]: x["values"][0] for x in rad.get("parameters", [])}
            v = {"t": p.get("t"), "p": p.get("pmean", 0), "w": p.get("ws"), "s": p.get("Wsymb2"),
                 "d": p.get("wd"), "g": p.get("gust")}
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
                           "w": round(float(v["w"] or 0), 1), "s": int(v["s"] or 0),
                           "d": None if v.get("d") is None else int(float(v["d"])),
                           "g": None if v.get("g") is None else round(float(v["g"]), 1)})
    return timmar or None


# ---------- Unikum-mejl från Gmail ----------
def text_ur_mejl(msg):
    t = texter_ur_mejl(msg)
    return t[0] if t else ""


def texter_ur_mejl(msg):
    """Mejlets text, först som vanlig text och sedan omgjord från HTML (Unikum skickar båda)."""
    ut = []
    del_text = msg.get_body(preferencelist=("plain",))
    if del_text is not None:
        try:
            ut.append(del_text.get_content())
        except Exception:  # noqa: BLE001
            pass
    del_html = msg.get_body(preferencelist=("html",))
    if del_html is not None:
        try:
            ut.append(_html_till_text(del_html.get_content()))
        except Exception:  # noqa: BLE001
            pass
    return [t for t in ut if t and t.strip()]


def _html_till_text(h):
    h = re.sub(r"(?is)<(script|style).*?</\1>", "", h)
    h = re.sub(r"(?i)<br\s*/?>|</?(p|div|tr|td|th|li|ul|ol|table|tbody|section|article|header|footer|h\d)\b[^>]*>", "\n", h)
    t = html.unescape(re.sub(r"<[^>]+>", "", h))
    return t.replace("\xa0", " ").replace("\u200b", "")


def _norm(t):
    return re.sub(r"\s+", " ", (t or "").replace("\xa0", " ")).strip().lower()


def tolka_unikum(rubrik, text):
    """Plockar ut grupprad och själva inlägget ur ett Unikum-mejl."""
    rader = [r.strip().lstrip(">").strip() for r in text.replace("\r", "").split("\n")]
    grupper, start = "", 0
    for i, r in enumerate(rader):
        mf = re.search(r"Från\s+.{1,80}?\s+till\s+", r)
        if mf:
            # Långa grupprader bryts ofta i vidarebefordrade mejl: ta med fortsättningen (korta rader utan meningar).
            delar, j = [r[mf.start():]], i + 1
            while (j < len(rader) and rader[j] and not re.match(r"(hej|hejsan|hallå|god ?morgon)\b", rader[j], re.I)
                   and (delar[-1].rstrip().endswith(",") or (len(rader[j]) < 60 and not re.search(r"[.!?:]", rader[j])))):
                delar.append(rader[j])
                j += 1
            grupper, start = " ".join(delar), j
            break
    if not grupper:
        # Reserv: en tidig rad som räknar upp mottagare, t.ex. "Mentor, Personal, Barn i Viljan Gul"
        for i, r in enumerate(rader[:15]):
            if re.match(r"^(från\s+\S+\s+till\s+)?((vårdnadshavare|mentor|pedagoger|personal|barn)[,\s]+)+.*\bi\b", r, re.I):
                grupper, start = r, i + 1
                break
    kropp = []
    for r in rader[start:]:
        if r.startswith("Ser inlägget konstigt ut") or r.startswith("Detta mail skickades"):
            break
        if r and r != "[bild]" and not r.startswith("[image"):
            kropp.append(r)
    if kropp and kropp[0] == rubrik:
        kropp = kropp[1:]
    return grupper, "\n".join(kropp).strip()


def barn_for(grupper, barn):
    if not grupper:
        return [b["namn"] for b in barn]
    g = _norm(grupper)
    return [b["namn"] for b in barn if _norm(b["grupp"]) and _norm(b["grupp"]) in g]


def hamta_unikum(adress, losen, barn, dagar=14):
    m = imaplib.IMAP4_SSL("imap.gmail.com")
    m.login(adress, losen)
    mapp = _allmail_mapp(m)
    typ, _ = m.select(mapp, readonly=True)
    if typ != "OK":
        mapp = "INBOX"
        m.select(mapp, readonly=True)
    logg(f"Gmail: läser mappen {mapp}")
    # Matchar både automatiskt vidarebefordrade (avsändare unikum.net) och manuellt vidarebefordrade ("via Unikum" i texten).
    # Bred sökning (även manuellt vidarebefordrade mejl där du själv står som avsändare). Skriptet sorterar ut riktiga inlägg nedan.
    typ, data = m.search(None, "X-GM-RAW", f'"unikum -filename:pdf newer_than:{dagar}d"')
    ids = data[0].split() if typ == "OK" and data and data[0] else []
    ut, utanfor, ej_inlagg = [], 0, 0
    traff_per_barn = [0] * len(barn)
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
        texter = texter_ur_mejl(msg)
        grupper, kropp, hur = "", "", ""
        for t in texter:  # prova vanlig text först, sedan HTML-versionen
            grupper, kropp = tolka_unikum(rubrik, t)
            if grupper:
                hur = "inlägg"
                break
        fran_unikum = "unikum.net" in str(msg.get("From", "")).lower() or any("via Unikum" in t for t in texter)
        if not grupper and fran_unikum and texter:
            # Inlägg direkt från Unikum där gruppraden inte gick att hitta: använd hela texten
            # och barnens grupper om de nämns någonstans, annars gäller det båda barnen.
            hel = max(texter, key=len)
            _, kropp = tolka_unikum(rubrik, hel)
            vem_i_text = [b_["namn"] for b_ in barn if _norm(b_["grupp"]) and _norm(b_["grupp"]) in _norm(hel)]
            grupper = " ".join(b_["grupp"] for b_ in barn if b_["namn"] in vem_i_text) or "alla"
            hur = "inlägg (utan grupprad)"
        typer = ",".join(sorted({p.get_content_type().split("/")[1] for p in msg.walk() if p.get_content_maintype() == "text"}))
        logg(f"  mejl {datum:%d/%m %H:%M} ({typer or '?'}): {hur or 'inget inlägg'}")
        if not grupper:
            ej_inlagg += 1
            continue  # nämner Unikum men är inget Unikum-inlägg
        vem = [b_["namn"] for b_ in barn] if grupper == "alla" else barn_for(grupper, barn)
        for k, b_ in enumerate(barn):
            if _norm(b_["grupp"]) in _norm(grupper):
                traff_per_barn[k] += 1
        if not vem:
            utanfor += 1
            continue  # gäller ingen av barnens grupper
        ut.append({"id": msg.get("Message-ID", str(i)), "rubrik": rubrik, "datum": datum.isoformat(),
                   "grupper": grupper, "barn": vem, "text": kropp[:9000]})
    m.logout()
    logg(f"Unikum: {len(ids)} mejl nämner Unikum senaste {dagar} dagarna, {ej_inlagg} var inga inlägg, {utanfor} gällde andra grupper, "
         f"{len(barn)} barn inlagda i BARN")
    for k, n in enumerate(traff_per_barn):
        logg(f"Unikum: barn {k + 1} i BARN – gruppen nämns i {n} mejl")
    ut.sort(key=lambda x: x["datum"], reverse=True)
    return ut


def _allmail_mapp(m):
    """Hittar Gmails 'Alla mejl'-mapp via flaggan \\All, oavsett språk på kontot."""
    try:
        typ, lst = m.list()
        for rad in lst if typ == "OK" else []:
            r = rad.decode("utf-8", "replace")
            if "\\All" in r:
                namn = r.rsplit(' "/" ', 1)[-1].strip()
                return namn if namn.startswith('"') else f'"{namn}"'
    except Exception:  # noqa: BLE001
        pass
    return "INBOX"


# ---------- AI-sammanfattning ----------
AI_INSTRUKTION = """Du hjälper en förälder inför förskolelämning. Nedan finns inlägg från förskolans Unikum (senaste veckan) och dagens datum.
"dagens_datum" är den dag föräldern förbereder sig för (lämning den dagen).
Svara ENDAST med JSON: {"inlagg":[{"id":"...","typ":"gor"|"info","kort":"...","galler_datum":"YYYY-MM-DD"|null}],
 "punkter":[{"text":"...","viktig":true|false}]}
- "gor": föräldern behöver göra, ta med eller tänka på något (ombyte, matsäck, stängt, utflykt, fotografering, ledig dag, blanketter).
- "info": bra att veta men ingen åtgärd.
- "kort": max 110 tecken på svenska, konkret, utan hälsningsfraser. Börja med det viktigaste.
- Långa veckobrev: leta efter inbjudningar, påminnelser och datum som gömmer sig i texten (t.ex. "Glöm inte att vi på måndag bjuder in till bokcafé").
  Finns något sådant är inlägget "gor" och "kort" ska handla om just det, inte om veckans aktiviteter.
- "galler_datum": datum för en kommande händelse eller deadline som inlägget handlar om (t.ex. bokcafé, stängt, utflykt), annars null.
  Sätt null för allmän information utan händelse (t.ex. sjukdom på förskolan, veckobrev). Tolka "på fredag" utifrån inläggets datum.
Ta med alla inlägg i "inlagg".

"punkter" är det som visas överst på föräldrarnas morgonskärm. Prioritera hårt:
- Högst 3 punkter, gärna färre. Inga punkter alls om inget är aktuellt (tom lista).
- Varje punkt max 70 tecken, en sak per punkt, skriven som en snabb notis. Ingen hälsning, ingen rubrik-upprepning.
- Ordning: 1) något föräldern måste göra eller ta med på dagens_datum eller nästa dag, 2) stängt/ändrade tider,
  3) pågående sjukdom eller smitta på förskolan (skickat senaste 3 dagarna), 4) kommande händelser senaste veckan.
- Ta INTE med händelser som redan har varit före dagens_datum, och inte allmänna veckobrev utan åtgärd.
- "viktig": true bara för sådant som kräver handling på dagens_datum eller dagen efter, eller stängt/smitta. Övrigt false.
- Skriv datum som veckodag (t.ex. "på fredag") när det hjälper."""


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
    svar_json = json.loads(text)
    punkter = [{"text": str(p.get("text", ""))[:90], "viktig": bool(p.get("viktig"))}
               for p in svar_json.get("punkter", []) if p.get("text")][:3]
    return {x["id"]: x for x in svar_json.get("inlagg", [])}, punkter


def forsta_mening(text):
    """Utan AI: första riktiga raden, utan hälsningsfras, max 110 tecken."""
    rader = [r.strip() for r in (text or "").split("\n") if r.strip()]
    rader = [r for r in rader if not re.fullmatch(r"(hej|hejsan|hallå|god ?morgon)\b.{0,25}[,!.]?", r, re.I)]
    t = " ".join(rader)
    t = re.sub(r"^(hej|hejsan|hallå|god ?morgon)[^,.!]{0,20}[,!.]\s*", "", t, flags=re.I)
    t = t[:1].upper() + t[1:]
    t = re.sub(r"https?://\S+", "", t).strip()
    return (t[:107] + "…") if len(t) > 110 else t


STARK = re.compile(r"\b(glöm inte|kom ihåg|bjuder in|välkomna? (till|på)|anmäl|senast|stängt|stänger|planeringsdag|"
                   r"studiedag|fotografering|föräldramöte|utvecklingssamtal)", re.I)
SVAG = re.compile(r"\b(ta med|utflykt|matsäck|ombyte|lediga?|ledighet)\b", re.I)
FRAMTID = re.compile(r"\b(imorgon|i morgon|nästa vecka|på (måndag|tisdag|onsdag|torsdag|fredag)|\d{1,2}/\d{1,2}|"
                     r"\d{1,2} (jan|feb|mar|apr|maj|jun|jul|aug|sep|okt|nov|dec))", re.I)


def paminnelser(text):
    """Utan AI: meningar som ser ut som påminnelser eller inbjudningar, starkast först."""
    meningar = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    ut = []
    for m in meningar:
        m = re.sub(r"\s*[:;]-?[)D(]+\s*$", "", m.strip())  # ta bort smileys
        if not 8 < len(m) < 220:
            continue
        poang = (2 if STARK.search(m) else 0) + (1 if SVAG.search(m) and FRAMTID.search(m) else 0) + (1 if FRAMTID.search(m) else 0)
        if poang >= 2:
            ut.append((poang, m.rstrip(".!") + "."))
    return [m for _, m in sorted(ut, key=lambda x: -x[0])]


def valj_inlagg(inlagg, ai, dag):
    """Behåll det som är relevant för måldagen."""
    ut = []
    for x in inlagg:
        a = ai.get(x["id"], {}) if ai else {}
        typ = a.get("typ", "info")
        kort = a.get("kort")
        if not a:
            pam = paminnelser(x["text"])
            if pam:
                typ, kort = "gor", pam[0][:200]
        kort = kort or forsta_mening(x["text"]) or x["rubrik"]
        galler = a.get("galler_datum")
        skickat = dt.date.fromisoformat(x["datum"][:10])
        if galler:
            try:
                g = dt.date.fromisoformat(galler)
                if g < dag and typ == "gor":
                    continue  # händelsen har redan varit
                if g < dag:
                    galler = None  # info: visa ändå, enligt skickat-datum
            except ValueError:
                galler = None
        if not galler and (dag - skickat).days > 7:
            continue
        ut.append({"rubrik": x["rubrik"], "kort": kort, "typ": typ, "barn": x["barn"],
                   "galler_datum": galler, "skickat": x["datum"][:10], "tid": x["datum"]})
    ut.sort(key=lambda x: x["tid"], reverse=True)  # senaste först
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


# ---------- Matsedel från PDF som mejlats till Gmail-kontot ----------
VARDAGAR = ["måndag", "tisdag", "onsdag", "torsdag", "fredag"]
MAN_KORT = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "maj": 5, "jun": 6, "jul": 7, "aug": 8,
            "sep": 9, "okt": 10, "nov": 11, "dec": 12}
SKRAP = re.compile(r"(skolmaten|sida \d|page \d|www\.|https?://|^vecka|^v\.? ?\d|^\d+ ?/ ?\d+$|^[\d\s.,/-]+$)", re.I)


def _ar_for(manad_eller_vecka, dag, vecka=False):
    """Gissa år när PDF:en inte anger det (hanterar årsskiftet)."""
    ref = dag.isocalendar()[1] if vecka else dag.month
    spann = 26 if vecka else 6
    if manad_eller_vecka < ref - spann:
        return dag.year + 1
    if manad_eller_vecka > ref + spann:
        return dag.year - 1
    return dag.year


def tolka_meny_text(text, dag):
    """Returnerar {datum: 'rätt / rätt'} ur texten i en utskriven skolmaten-meny."""
    meny, vecka, aktuell, ratter = {}, None, None, []

    def spara():
        ratter[:] = [r for r in ratter if not re.fullmatch(r"menyn saknas\.?", r, re.I)]
        if aktuell and ratter:
            meny[aktuell] = " ".join(dict.fromkeys(ratter))[:200]

    for rad in (r.strip() for r in text.replace("\r", "").split("\n")):
        if not rad:
            continue
        mv = re.search(r"\b(?:vecka|v\.?)\s*(\d{1,2})\b", rad, re.I)
        md = re.match(r"^(måndag|tisdag|onsdag|torsdag|fredag|mån|tis|ons|tor|fre)\b[\s.,:-]*(.*)$", rad, re.I)
        if md:
            spara()
            ratter = []
            wd = next(i for i, n in enumerate(VARDAGAR) if n.startswith(md.group(1).lower()[:3]))
            rest = md.group(2)
            aktuell = None
            iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", rest)
            dm = re.search(r"(\d{1,2})\s*(?:/\s*(\d{1,2})|\s+([a-zåäö]{3})[a-zåäö]*\.?)", rest, re.I)
            try:
                if iso:
                    aktuell = dt.date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
                    rest = rest.replace(iso.group(0), "")
                elif dm and (dm.group(2) or (dm.group(3) or "").lower()[:3] in MAN_KORT):
                    manad = int(dm.group(2)) if dm.group(2) else MAN_KORT[dm.group(3).lower()[:3]]
                    aktuell = dt.date(_ar_for(manad, dag), manad, int(dm.group(1)))
                    rest = rest.replace(dm.group(0), "")
                elif vecka:
                    aktuell = dt.date.fromisocalendar(_ar_for(vecka, dag, vecka=True), vecka, wd + 1)
            except ValueError:
                aktuell = None
            rest = rest.strip(" -–:,.")
            if rest and not SKRAP.search(rest):
                ratter.append(rest)
            continue
        if mv and len(rad) < 40:
            spara()
            vecka, aktuell, ratter = int(mv.group(1)), None, []
            continue
        if aktuell and not SKRAP.search(rad) and len(ratter) < 3:
            ratter.append(rad)
    spara()
    return meny


def pdf_text(data):
    """Läser PDF:en kolumn för kolumn (skolmatens utskrift har två veckor bredvid varandra)."""
    import io
    import logging
    import pdfplumber  # installeras i workflow-filen
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    delar = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for sida in pdf.pages:
            mitt = sida.width / 2
            for box in ((0, 0, mitt, sida.height), (mitt, 0, sida.width, sida.height)):
                delar.append(sida.crop(box).extract_text() or "")
    return "\n".join(delar)


def ai_meny(text, dag, nyckel, modell):
    fraga = (f"Här är texten ur en matsedel för en förskola. Vilken lunch serveras {VECKODAGAR[dag.weekday()]} {dag.isoformat()}? "
             "Svara ENDAST med rätten/rätterna på en rad, eller med ordet SAKNAS om dagen inte finns i menyn.\n\n" + text[:12000])
    kropp = json.dumps({"model": modell, "max_tokens": 200, "messages": [{"role": "user", "content": fraga}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=kropp, method="POST", headers={
        "x-api-key": nyckel, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        svar = json.loads(r.read().decode())
    t = "".join(b.get("text", "") for b in svar.get("content", [])).strip()
    return None if not t or "SAKNAS" in t.upper() else t[:200]


def hamta_matsedel_pdf(adress, losen, dag, ai_nyckel=None, modell=None):
    m = imaplib.IMAP4_SSL("imap.gmail.com")
    m.login(adress, losen)
    mapp = _allmail_mapp(m)
    if m.select(mapp, readonly=True)[0] != "OK":
        m.select("INBOX", readonly=True)
    q = '"(subject:skolmat OR subject:skolmaten OR subject:meny OR subject:matsedel) filename:pdf newer_than:60d"'
    typ, data = m.search(None, "X-GM-RAW", q)
    ids = data[0].split() if typ == "OK" and data and data[0] else []
    logg(f"Matsedel: {len(ids)} mejl med meny-PDF senaste 60 dagarna")
    for i in reversed(ids[-5:]):  # nyaste först
        typ, d = m.fetch(i, "(RFC822)")
        if typ != "OK":
            continue
        msg = email.message_from_bytes(d[0][1], policy=email.policy.default)
        for del_ in msg.iter_attachments():
            namn = (del_.get_filename() or "").lower()
            if del_.get_content_type() != "application/pdf" and not namn.endswith(".pdf"):
                continue
            text = pdf_text(del_.get_payload(decode=True))
            meny = tolka_meny_text(text, dag)
            veckor = sorted({x.isocalendar()[1] for x in meny})
            logg(f"Matsedel: PDF med {len(text)} tecken, {len(meny)} dagar tolkade (vecka {veckor or '–'})")
            if dag in meny:
                m.logout()
                return meny[dag]
            if ai_nyckel:
                svar = ai_meny(text, dag, ai_nyckel, modell)
                if svar:
                    logg("Matsedel: dagen hittades med AI")
                    m.logout()
                    return svar
    m.logout()
    logg(f"Matsedel: {dag} finns inte i någon PDF – dags att mejla en ny meny?")
    return None


# ---------- Kryptering (läses av docs/index.html med Web Crypto) ----------
def kryptera(obj, losen):
    salt, iv = secrets.token_bytes(16), secrets.token_bytes(12)
    nyckel = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITER).derive(losen.encode())
    ct = AESGCM(nyckel).encrypt(iv, json.dumps(obj, ensure_ascii=False).encode(), None)
    b = lambda x: base64.b64encode(x).decode()  # noqa: E731
    return {"v": 1, "iter": PBKDF2_ITER, "salt": b(salt), "iv": b(iv), "data": b(ct)}


def namnsdagar(dag, bevakade_text):
    """Dagens namn och nästa namnsdag bland de bevakade namnen."""
    lista = json.loads((ROT / "namnsdagar.json").read_text(encoding="utf-8"))["namn"]
    idag = lista.get(dag.strftime("%m-%d"), [])
    bevakade = [n.strip() for n in re.split(r"[,;\n]", bevakade_text or "") if n.strip()]
    kommande = []
    for namn in bevakade:
        for steg in range(0, 367):
            d = dag + dt.timedelta(days=steg)
            traff = [x for x in lista.get(d.strftime("%m-%d"), []) if x.lower() == namn.lower()]
            if traff:
                kommande.append({"namn": traff[0], "datum": d.isoformat()})
                break
        else:
            logg(f"Namnsdag: hittade inget datum för namn nr {bevakade.index(namn) + 1} – stavning?")
    kommande.sort(key=lambda x: x["datum"])
    return idag, kommande


def las_barn(s):
    barn = []
    for del_ in (s or "").split(";"):
        if "=" in del_:
            namn, grupp = del_.split("=", 1)
            barn.append({"namn": namn.strip(), "grupp": grupp.strip()})
    return barn


def main():
    # Hemligheter som klistrats in med radbrytning eller mellanslag i slutet ställer annars till det.
    for k in ("GMAIL_ADRESS", "GMAIL_APPLOSENORD", "ANTHROPIC_API_KEY", "SKOLMATEN_TOKEN", "SKOLMATEN_SKOLA", "BARN", "NAMNSDAGAR"):
        if os.environ.get(k):
            os.environ[k] = os.environ[k].strip()
    inst = json.loads((ROT / "installningar.json").read_text(encoding="utf-8"))
    barn = las_barn(os.environ.get("BARN"))
    if not barn:
        logg("OBS: hemligheten BARN saknas eller har fel format. Exempel: Ella=Tryggheten Rosa;Nils=Viljan Gul")
    nu = dt.datetime.now(TZ)
    dag, etikett = maldag(nu)
    fel = []

    timmar = None
    try:
        timmar = hamta_vader(inst["plats"]["lat"], inst["plats"]["lon"], dag)
    except Exception as e:  # noqa: BLE001
        fel.append("vader"); logg("Väder:", e)

    unikum, punkter = [], None
    if os.environ.get("GMAIL_ADRESS") and os.environ.get("GMAIL_APPLOSENORD"):
        try:
            rå = hamta_unikum(os.environ["GMAIL_ADRESS"], os.environ["GMAIL_APPLOSENORD"].replace(" ", ""), barn)
            logg(f"Unikum: {len(rå)} inlägg för barnens grupper")
            ai = None
            if rå and not os.environ.get("ANTHROPIC_API_KEY"):
                logg("AI: ingen ANTHROPIC_API_KEY – använder enkel tolkning")
            if rå and os.environ.get("ANTHROPIC_API_KEY"):
                try:
                    ai, punkter = ai_sammanfatta(rå, dag, os.environ["ANTHROPIC_API_KEY"], inst.get("ai_modell", "claude-haiku-4-5"))
                    logg(f"AI: {len(ai)} av {len(rå)} Unikum-inlägg tolkade, {len(punkter)} punkter till gula rutan")
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
    if not matsedel and os.environ.get("GMAIL_ADRESS") and os.environ.get("GMAIL_APPLOSENORD"):
        try:
            matsedel = hamta_matsedel_pdf(os.environ["GMAIL_ADRESS"], os.environ["GMAIL_APPLOSENORD"].replace(" ", ""), dag,
                                          os.environ.get("ANTHROPIC_API_KEY"), inst.get("ai_modell", "claude-haiku-4-5"))
        except ImportError:
            fel.append("matsedel"); logg("Matsedel: pdfplumber saknas – lägg till den på pip install-raden i workflow-filen")
        except Exception as e:  # noqa: BLE001
            fel.append("matsedel"); logg("Matsedel (PDF):", e)

    namn_idag, namn_kommande = [], []
    try:
        namn_idag, namn_kommande = namnsdagar(dag, os.environ.get("NAMNSDAGAR"))
    except Exception as e:  # noqa: BLE001
        logg("Namnsdagar:", e)

    innehall = {
        "dag": dagtext(dag, etikett), "datum": dag.isoformat(),
        "relativ": etikett.lower() if etikett else "på " + VECKODAGAR[dag.weekday()], "hamtad": nu.strftime("%-d/%-m %H:%M"),
        "barn": [b["namn"] for b in barn], "lamning": inst["lamning"], "hamtning": inst["hamtning"],
        "timmar": timmar, "namnsdag": namn_idag, "bevakade_namnsdagar": namn_kommande, "unikum": unikum, "unikum_punkter": punkter, "matsedel": matsedel, "fel": fel,
    }
    losen = os.environ.get("SIDANS_LOSENORD")
    if not losen:
        sys.exit("SIDANS_LOSENORD saknas – lägg till den som hemlighet i GitHub.")
    (ROT / "docs" / "data.json").write_text(json.dumps(kryptera(innehall, losen)), encoding="utf-8")
    logg(f"Klart: {innehall['dag']}, {len(timmar or [])} väder-timmar, {len(unikum)} Unikum-rader, "
         f"matsedel {'ja' if matsedel else 'nej'}, fel: {fel or 'inga'}")


if __name__ == "__main__":
    main()
