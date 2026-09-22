"""
Monthly SLCheck data refresh.

Downloads the current Spezialitaetenliste from BAG/FOPH (FHIR NDJSON export,
German + French), converts it to the compact format SLCheck uses, and writes
it into index.html together with the new date and count.

It refuses to write anything if the download looks wrong, so a bad month
from BAG can never break the live site: the GitHub Action simply fails and
GitHub emails you.
"""
import datetime as dt
import gzip
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict

BASE = os.environ.get(
    "SL_BASE_URL",
    "https://epl.bag.admin.ch/static/sl/publication/fhir/foph-sl-publication-latest-{lang}.ndjson",
)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "index.html")
FORMS = os.path.join(ROOT, "scripts", "dose_forms.json")

PRICE_PUBLIC = "756002005001"
GENERIC = "756001003001"
LIMIT_TEXT_MAX = 250
MIN_RECORDS = 9000          # the SL has ~10,400 packs; far fewer means a broken file
MAX_SHRINK = 0.10           # refuse if more than 10% of drugs vanish in one month

DB_RE = re.compile(r'(<script type="application/json" id="sl-db">)(.*?)(</script>)', re.S)


def fail(msg):
    print("UPDATE STOPPED:", msg)
    sys.exit(1)


# ---------------------------------------------------------------- download
def download(lang):
    url = BASE.format(lang=lang)
    print("Downloading", url)
    req = urllib.request.Request(url, headers={"User-Agent": "SLCheck-updater (github.com/abbie122/slcheck)"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            raw = r.read()
    except Exception as e:
        fail(f"could not download {url} ({e}). BAG may have moved the file again.")
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8")
    if not text.lstrip().startswith("{"):
        fail(f"{url} did not return FHIR data (got a web page instead). BAG may have moved the file.")
    return text


# ---------------------------------------------------------------- parsing
def ext(items, name):
    for e in items or []:
        if e.get("url") == name or e.get("url", "").endswith("/" + name):
            return e
    return None


def coding_code(cc):
    return (cc or {}).get("coding", [{}])[0].get("code")


def parse(ndjson_text, today):
    """Return {gtin: record} for every reimbursed pack in one language file."""
    out = {}
    for line in ndjson_text.splitlines():
        if not line.strip():
            continue
        bundle = json.loads(line)
        res = [e["resource"] for e in bundle.get("entry", [])]
        mpd = next((r for r in res if r["resourceType"] == "MedicinalProductDefinition"), None)
        if not mpd:
            continue
        name = (mpd.get("name") or [{}])[0].get("productName", "")
        atc, generic = "", False
        for cls in mpd.get("classification", []):
            for c in cls.get("coding", []):
                if "atc" in c.get("system", "").lower() and not atc:
                    atc = c.get("code", "")
                if c.get("code") == GENERIC:
                    generic = True
        substances = ", ".join(
            r["substance"]["code"]["concept"]["text"]
            for r in res
            if r["resourceType"] == "Ingredient"
            and r.get("substance", {}).get("code", {}).get("concept", {}).get("text")
        )
        cud_text = {
            r["id"]: r.get("indication", {}).get("diseaseSymptomProcedure", {}).get("concept", {}).get("text", "")
            for r in res if r["resourceType"] == "ClinicalUseDefinition"
        }
        packs = {r["id"]: r for r in res if r["resourceType"] == "PackagedProductDefinition"}

        for ra in res:
            if ra["resourceType"] != "RegulatedAuthorization":
                continue
            if coding_code(ra.get("type")) != "756000002003":   # Reimbursement SL
                continue
            ref = (ra.get("subject") or [{}])[0].get("reference", "")
            pack = packs.get(ref.rsplit("/", 1)[-1])
            if not pack:
                continue
            gtin = (pack.get("packaging", {}).get("identifier") or [{}])[0].get("value")
            if not gtin:
                continue

            reimb = ext(ra.get("extension"), "reimbursementSL")
            sub = reimb.get("extension", []) if reimb else []
            cost = next((e.get("valueInteger") for e in sub if e.get("url") == "costShare"), 10)
            price = 0.0
            for e in sub:
                if "productPrice" in e.get("url", ""):
                    parts = e.get("extension", [])
                    t = coding_code(next((p.get("valueCodeableConcept") for p in parts if p["url"] == "type"), None))
                    v = next((p.get("valueMoney", {}).get("value") for p in parts if p["url"] == "value"), None)
                    if t == PRICE_PUBLIC and v is not None:
                        price = float(v)

            texts = []
            for ind in ra.get("indication", []):
                lim = ext(ind.get("extension"), "regulatedAuthorization-limitation")
                if not lim:
                    continue
                le = lim.get("extension", [])
                end = next((e.get("valuePeriod", {}).get("end") for e in le if e["url"] == "period"), None)
                if end and end < today:
                    continue                                   # expired limitation
                txt = next((e.get("valueString") for e in le if e["url"] == "limitationText"), None)
                if not txt:
                    ref = next((e.get("valueReference", {}).get("reference", "") for e in le
                                if e["url"] == "limitationIndication"), "")
                    txt = cud_text.get(ref.rsplit("/", 1)[-1], "")
                if txt:
                    texts.append(" ".join(txt.split()))

            desc = pack.get("description") or name
            out[gtin] = {
                "product": name, "desc": desc, "s": substances, "a": atc,
                "p": round(price, 2), "c": cost, "ig": generic,
                "lim": " ".join(texts)[:LIMIT_TEXT_MAX],
                "has_lim": bool(texts),
            }
    return out


# ------------------------------------------------ name in "Brand, Form, Pack"
def display_name(rec, old_brand, forms):
    product, desc = rec["product"].strip(), rec["desc"].strip()
    pack = desc[len(product):].strip(" ,") if product and desc.startswith(product) else ""
    words = product.split()
    brand = None
    if old_brand and product.startswith(old_brand + " "):
        brand = old_brand
    else:
        for i, w in enumerate(words[1:], start=1):
            if w in forms:
                brand = " ".join(words[:i])
                break
    if not brand:
        brand = words[0] if words else product
    form = product[len(brand):].strip(" ,")
    return ", ".join(p for p in (brand, form, pack) if p)


# ---------------------------------------------------------------- main
MONTHS = {
    "en": "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(),
    "en_long": "January February March April May June July August September October November December".split(),
    "de": "Jan. Feb. März Apr. Mai Juni Juli Aug. Sept. Okt. Nov. Dez.".split(),
    "fr": "janv. févr. mars avr. mai juin juil. août sept. oct. nov. déc.".split(),
    "it": "gen. feb. mar. apr. mag. giu. lug. ago. set. ott. nov. dic.".split(),
}


def any_month(key):
    return "(?:" + "|".join(re.escape(m) for m in MONTHS[key]) + ")"


def main():
    today = dt.date.today()
    html = open(INDEX, encoding="utf-8").read()
    m = DB_RE.search(html)
    if not m:
        fail("could not find the sl-db data block in index.html")
    old = json.loads(m.group(2))
    old_brand = {x["g"]: x["n"].split(",")[0] for x in old}
    forms = set(json.load(open(FORMS, encoding="utf-8")))

    de = parse(download("de"), today.isoformat())
    fr = parse(download("fr"), today.isoformat())

    db = []
    for gtin, r in de.items():
        rec = {
            "n": display_name(r, old_brand.get(gtin), forms),
            "s": r["s"], "a": r["a"], "g": gtin, "p": r["p"], "c": r["c"], "l": r["has_lim"],
        }
        if r["ig"]:
            rec["ig"] = True
        if r["has_lim"]:
            rec["ld"] = r["lim"]
            rec["lf"] = fr.get(gtin, {}).get("lim") or r["lim"]
        db.append(rec)
    db.sort(key=lambda x: x["n"].lower())

    # cheapest generic in the same ATC class, and the saving per pack
    cheapest = {}
    for x in db:
        if x.get("ig") and x["p"] > 0 and x["a"]:
            if x["a"] not in cheapest or x["p"] < cheapest[x["a"]]["p"]:
                cheapest[x["a"]] = x
    for x in db:
        c = cheapest.get(x["a"])
        if c and not x.get("ig"):
            saving = round(x["p"] - c["p"], 2)
            if saving > 0:
                x["gn"], x["gs"] = c["n"], saving

    # ---- safety checks: never publish a broken month
    n = len(db)
    if n < MIN_RECORDS:
        fail(f"only {n} drugs parsed (expected ~10,000). Not updating.")
    if n < len(old) * (1 - MAX_SHRINK):
        fail(f"drug count fell from {len(old)} to {n}. Not updating; check BAG's file by hand.")
    if sum(1 for x in db if x["p"] > 0) < n * 0.9:
        fail("more than 10% of drugs have no price. Not updating.")

    if {x["g"]: x for x in db} == {x["g"]: x for x in old}:
        print("BAG data unchanged since last update. Nothing to do.")
        return

    payload = json.dumps(db, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")
    html = html[:m.start(2)] + payload + html[m.end(2):]

    mi = today.month - 1
    count = f"{n:,}"
    long_date = f"{today.day} {MONTHS['en_long'][mi]} {today.year}"
    subs = [
        (r"Loading [\d,]+ medications…", f"Loading {count} medications…"),
        (r"— [\d,]+ medications, extract of \d{1,2} \w+ \d{4}", f"— {count} medications, extract of {long_date}"),
        (r"\(BAG/FOPH\), extract of \d{1,2} \w+ \d{4}", f"(BAG/FOPH), extract of {long_date}"),
        (r"BAG/FOPH " + any_month("en") + r" \d{4}", f"BAG/FOPH {MONTHS['en'][mi]} {today.year}"),
        (r"· BAG " + any_month("de") + r" \d{4}", f"· BAG {MONTHS['de'][mi]} {today.year}"),
        (r"OFSP " + any_month("fr") + r" \d{4}", f"OFSP {MONTHS['fr'][mi]} {today.year}"),
        (r"UFSP " + any_month("it") + r" \d{4}", f"UFSP {MONTHS['it'][mi]} {today.year}"),
    ]
    for pattern, new in subs:
        html, k = re.subn(pattern, new, html)
        if k == 0:
            fail(f"could not find the text to update: {pattern}")

    open(INDEX, "w", encoding="utf-8").write(html)
    added = len(set(x["g"] for x in db) - set(x["g"] for x in old))
    removed = len(set(x["g"] for x in old) - set(x["g"] for x in db))
    print(f"Updated: {n} drugs (+{added} new, -{removed} delisted), dated {long_date}.")


if __name__ == "__main__":
    main()
