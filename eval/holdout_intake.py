"""Offline check of a pulled lead CSV before any spend (stage F, protocol section 3).

    # 1. whole pull, no sample: report + eyeball list for your skim
    python eval/holdout_intake.py data/atlanta/intake/atlanta_pull_raw.csv --out data/atlanta/intake \
        --pull-date 2026-11-15
    # 2. after the skim and any denylist commit: draw the sample from the filtered population
    python eval/holdout_intake.py data/atlanta/intake/atlanta_pull_raw.csv --out data/atlanta/intake \
        --pull-date 2026-11-15 --sample 45 --seed 20261011 --skimmed

Free and offline: no network, no keys, never imports the pipeline graph. It reads the raw CSV (never modifies
it) and, read-only, the Charlotte leads.db.

What it does, in order (the population rule is the 2026-11-07 amendment in eval/HOLDOUT_PROTOCOL.md):
  realigns a phantom leading column (headers shifted against the data), strips a byte-order mark;
  needs a qualified_lead column (STOPS without it); counts its values over ALL rows, normalized (trimmed,
  lowercase): true, false, blank; STOPS on any other value, naming the value and its rows but no business;
  STOPS if any Place ID is also in the Charlotte database (wrong city or wrong file; sees all rows);
  STOPS if rows sharing a Place ID disagree on qualified_lead (blank versus false counts as disagreeing);
  drops rows with no name or no Place ID and duplicate Place IDs (first kept), counting them over all rows;
  keeps only the rows whose qualified_lead is true (the population); every later step sees those rows only;
  prints and writes a counts block: raw pull, workflow, pull date, removed, remaining, qualified, not qualified;
  drops unparsable addresses and other states;
  applies the pipeline's own intake rule (the '@' rule and the Place ID denylist);
  flags chain-name overlaps with Charlotte (kept), same-name different-Place-ID leads, shared phone/address,
  and names that look like a person or a mobile/independent barber (the eyeball list);
  writes atlanta_leads_filtered.csv (the whole filtered pull). Only with --sample does it also write
  atlanta_leads_clean.csv (the list that is run) and sample.txt.

A row that is not qualified (false or blank) is only counted: its business name and Place ID appear in no file
under --out, in the report or on the console, whatever else would have happened to it.

Outputs go in --out, which must be a folder under <repo>/data/ (gitignored). They hold real business names.
Exit code 0 = ok, 2 = stopped (reason in the report).
"""
import argparse
import csv
import datetime
import io
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
import holdout_common as hc                      # noqa: E402
import intake                                    # noqa: E402
import paths                                     # noqa: E402
import setup_database as sdb                     # noqa: E402  (parse_city_state only)

ALIASES = {
    "business_name": ("business_name", "business name", "name"),
    "address": ("address", "formatted_address", "full address"),
    "phone": ("phone", "phone_number", "phone number"),
    "id": ("id", "place_id", "place id", "google place id", "googleplaceid"),
    "website": ("website", "website_url", "site"),
    "google_url": ("google_url", "google url", "maps_url", "google maps url"),
    "qualified_lead": ("qualified_lead", "qualified lead"),
}
REQUIRED = ("business_name", "address", "id", "qualified_lead")
QUALIFIED_VALUES = ("true", "false", "")
DEFAULT_WORKFLOW = "Ai Lead Qualification"
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
N8N_RE = re.compile(r"decision|first_name|last_name|owner|email|outreach|pitch|confidence|hunter|dm_flag|persons?\b", re.I)

PLACE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")
ADDRESS_RE = re.compile(r",\s*[A-Z]{2}\s+\d{5}")
URL_RE = re.compile(r"^(https?://)?[\w.-]+\.[a-z]{2,}(:\d+)?(/\S*)?$", re.I)

BOOKING = ("booksy.com", "booksy.net", "thecut.co", "vagaro.com", "fresha.com", "squareup.com", "square.site",
           "styleseat.com", "glossgenius.com", "schedulicity.com", "acuityscheduling.com", "setmore.com",
           "mindbodyonline.com", "getsquire.com", "trafft.com")
SOCIAL = ("facebook.com", "instagram.com", "tiktok.com", "linktr.ee", "twitter.com", "x.com", "youtube.com",
          "yelp.com", "google.com", "business.site")

SHOP_WORDS = {"barber", "barbers", "barbershop", "barbershops", "barbering", "shop", "cuts", "cut", "cutz", "kutz",
              "kuts", "fade", "fades", "clippers", "clipper", "salon", "grooming", "groomers", "hair", "haircuts",
              "haircut", "lounge", "studio", "studios", "parlor", "parlour", "blade", "blades", "razor", "trim",
              "trims", "styles", "stylez", "gentlemen", "gentleman", "gents", "chair", "chairs", "edge", "edges",
              "lineup", "line", "kings", "queens", "men", "mens", "mans", "man", "club", "co", "company", "spa",
              "emporium", "den", "cave", "room", "lab", "factory", "institute", "academy", "collective", "bar"}
SOLO_RE = re.compile(r"\b(mobile|suite|ste\b|booth|inside|independent|freelance|rental|house ?calls?|by appointment|"
                     r"home ?based|private)\b", re.I)


# --------------------------------------------------------------------------------------------------
def domain_of(url):
    u = (url or "").strip()
    if not u:
        return ""
    if "//" not in u:
        u = "http://" + u
    host = (urlparse(u).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def website_class(url):
    d = domain_of(url)
    if not d:
        return "blank"
    if any(d == b or d.endswith("." + b) for b in BOOKING):
        return "booking"
    if any(d == s or d.endswith("." + s) for s in SOCIAL):
        return "social"
    return "own"


def _looks_phone(v):
    d = re.sub(r"\D", "", v)
    return 10 <= len(d) <= 11 and len(v) <= 25


def _passes(col, v):
    v = v.strip()
    if col == "id":
        return bool(PLACE_ID_RE.match(v))
    if col == "address":
        return bool(ADDRESS_RE.search(v)) or v.count(",") >= 2
    if col == "phone":
        return _looks_phone(v)
    if col == "website":
        return bool(URL_RE.match(v)) and " " not in v
    if col == "business_name":
        return not PLACE_ID_RE.match(v) and not _looks_phone(v) and not URL_RE.match(v)
    return True


def detect_shift(header_index, data, report):
    """Find the offset s such that the value for the header at index i sits at row[i + s].

    Returns (shift, score_by_shift). Tests 0, +1, -1 over id, address, phone, website and business_name; a column
    needs at least one non-blank cell to count. 0 wins if it scores 0.9 or more; otherwise the best of the others
    at 0.9 or more; otherwise None (stop)."""
    cols = [c for c in ("id", "address", "phone", "website", "business_name") if c in header_index]
    scores = {}
    for s in (0, 1, -1):
        fr = []
        for c in cols:
            vals = []
            for row in data:
                j = header_index[c] + s
                v = row[j].strip() if 0 <= j < len(row) else ""
                if v:
                    vals.append(v)
            if vals:
                fr.append(sum(_passes(c, v) for v in vals) / len(vals))
        scores[s] = sum(fr) / len(fr) if fr else 0.0
    if scores[0] >= 0.9:
        return 0, scores
    best = max((1, -1), key=lambda k: scores[k])
    return (best if scores[best] >= 0.9 else None), scores


def _street_key(address):
    first = (address or "").split(",")[0].lower()
    zip_ = re.findall(r"\b\d{5}\b", address or "")
    return (re.sub(r"[^a-z0-9]", "", first) + (zip_[-1] if zip_ else "")) if first.strip() else ""


def eyeball_reasons(rec, by_phone, by_street):
    """Why this lead might be a person-as-business record. Returns a list of short reasons (empty = nothing odd)."""
    name = rec["business_name"]
    words = [w for w in re.findall(r"[A-Za-z']+", name)]
    lower = {w.lower().strip("'") for w in words}
    reasons = []
    if not (lower & SHOP_WORDS):
        reasons.append("no shop word in the name")
        if 2 <= len(words) <= 3 and all(w[:1].isupper() for w in words):
            reasons.append("looks like a person's name")
    if re.search(r"\bthe barber\b", name, re.I) and len(words) <= 4:
        reasons.append("'the Barber' style personal brand")
    if SOLO_RE.search(name):
        reasons.append("mobile / independent / suite wording")
    cls = website_class(rec["website"])
    if cls in ("booking", "social"):
        reasons.append(f"only a {cls} page as website")
    p = re.sub(r"\D", "", rec["phone"])[-10:]
    if p and len(by_phone.get(p, ())) > 1:
        reasons.append("shares phone with: " + "; ".join(sorted(n for n in by_phone[p] if n != name)[:3] or ["a lead of the same name"]))
    sk = _street_key(rec["address"])
    if sk and len(by_street.get(sk, ())) > 1:
        reasons.append("shares address with: " + "; ".join(sorted(n for n in by_street[sk] if n != name)[:3] or ["a lead of the same name"]))
    return reasons


def place_ids_readonly(db_path):
    uri = "file:" + os.path.abspath(db_path).replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT place_id, business_name FROM leads") if r[0]}
    finally:
        conn.close()


def intake_py_committed():
    """True if src/intake.py has no uncommitted change (so the denylist is in git before the draw); None if git is unavailable."""
    try:
        r = subprocess.run(["git", "-C", ROOT, "status", "--porcelain", "--", "src/intake.py"],
                           capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    return r.stdout.strip() == "" if r.returncode == 0 else None


# --------------------------------------------------------------------------------------------------
def run(raw_path, out_dir, sample=None, seed=hc.PROTOCOL_SEED, skimmed=False, charlotte_db=None, state="GA", echo=print,
        workflow=DEFAULT_WORKFLOW, pull_date=None):
    """Validate, filter, flag, and (with sample) draw. Returns the exit code (0 ok, 2 stopped).

    workflow and pull_date are only named in the report block (pull_date: a date or a YYYY-MM-DD string)."""
    R = []                                           # report lines

    def say(line=""):
        R.append(line)

    def finish(code):
        text = "\n".join(R) + "\n"
        try:
            with open(os.path.join(folder, "validation_report.txt"), "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass
        echo(text)
        return code

    try:
        folder = paths.resolve_data_dir(out_dir)
    except ValueError as e:
        echo(f"STOP: {e}")
        return 2
    os.makedirs(folder, exist_ok=True)

    say(f"Stage F intake validation   raw file: {os.path.basename(raw_path)}   (offline, no network, no keys)")
    say(f"pre-registered: sample {hc.SAMPLE_SIZE}, seed {hc.PROTOCOL_SEED}, drawn AFTER the skim (eval/HOLDOUT_PROTOCOL.md section 3)")

    if sample is not None and not skimmed:
        say("STOP: --sample needs --skimmed. The sample is drawn AFTER your eyeball skim and any denylist commit, never before.")
        return finish(2)
    if sample is not None:
        committed = intake_py_committed()
        if committed is False:
            say("STOP: src/intake.py has uncommitted changes. Commit the denylist lines (their own commit) BEFORE the draw.")
            return finish(2)
        if committed is None:
            say("WARNING: could not check git; make sure the denylist commit is done before this draw.")
        if seed != hc.PROTOCOL_SEED:
            say(f"WARNING: seed {seed} is not the pre-registered seed {hc.PROTOCOL_SEED}.")

    # ---- read ----------------------------------------------------------------------------------
    raw = open(raw_path, "rb").read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
        say("NOTE: file is not UTF-8; read as Windows-1252.")
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if len(rows) < 2:
        say("STOP: the file has no data rows.")
        return finish(2)
    header, data = rows[0], rows[1:]
    say(f"read {len(data)} data rows, {len(header)} header columns" + ("; byte-order mark removed" if bom else ""))

    norm = [h.strip().lower() for h in header]
    header_index = {}
    for canon, names in ALIASES.items():
        for nm in names:
            if nm in norm:
                header_index[canon] = norm.index(nm)
                break
    missing = [c for c in REQUIRED if c not in header_index]
    if missing:
        say(f"STOP: required column(s) not found by header: {', '.join(missing)}. Headers seen: {', '.join(h for h in header if h.strip())}")
        return finish(2)
    for opt in ("phone", "website", "google_url"):
        if opt not in header_index:
            say(f"NOTE: no '{opt}' column; treated as blank for every lead.")

    # ---- phantom leading column ------------------------------------------------------------------
    shift, scores = detect_shift(header_index, data, say)
    say("column alignment check (share of cells that look right, by offset): "
        + ", ".join(f"{s:+d}: {v:.0%}" for s, v in scores.items()))
    if shift is None:
        say("STOP: no offset aligns the headers with the data (need 90%). Open the file and look at the first rows.")
        return finish(2)
    if shift != 0:
        say(f"PHANTOM COLUMN: headers are shifted by {shift:+d} against the data; realigned on business_name.")
    else:
        say("alignment OK (no phantom column).")

    def cell(row, col):
        if col not in header_index:
            return ""
        j = header_index[col] + shift
        return row[j].strip() if 0 <= j < len(row) else ""

    records = [{"row": i + 2, "business_name": cell(r, "business_name"), "website": cell(r, "website"),
                "address": cell(r, "address"), "phone": cell(r, "phone"), "id": cell(r, "id"),
                "google_url": cell(r, "google_url"), "q": cell(r, "qualified_lead").strip().lower()} for i, r in enumerate(data)]

    # qualified_lead, counted over ALL rows before any dedupe or drop (counts only: no names, no Place IDs)
    qcount = Counter(rec["q"] for rec in records)
    say(f"qualified_lead values, all {len(records)} rows: true {qcount['true']}, false {qcount['false']}, blank {qcount['']}")
    unexpected = {}
    for rec in records:
        if rec["q"] not in QUALIFIED_VALUES:
            unexpected.setdefault(rec["q"], []).append(rec["row"])
    if unexpected:
        say("STOP: qualified_lead holds a value other than true, false or blank. Fix the pull (or tell me what the value means) "
            "before anything is filtered; nothing was written except this report.")
        for val, rows_at in sorted(unexpected.items(), key=lambda kv: kv[1][0]):
            more = "" if len(rows_at) <= 10 else f" (first 10 of {len(rows_at)})"
            say(f"  {val[:40]!r}: {len(rows_at)} row(s), rows {', '.join(str(n) for n in rows_at[:10])}{more}")
        return finish(2)

    n8n_cols = [h for h in header if h.strip() and N8N_RE.search(h) and norm.index(h.strip().lower()) not in
                {header_index[c] for c in header_index}]
    filled = {}
    for h in n8n_cols:
        j = norm.index(h.strip().lower()) + shift
        n = sum(1 for r in data if 0 <= j < len(r) and r[j].strip())
        if n:
            filled[h] = n
    if filled:
        say("WARNING: n8n answer/outreach columns carry values: " + ", ".join(f"{h} ({n})" for h, n in filled.items())
            + ". They are NOT copied to the clean CSV, so the run stays cold; do not paste them into the worksheet.")

    # ---- Charlotte overlap (STOP) ----------------------------------------------------------------
    charlotte_db = charlotte_db if charlotte_db is not None else paths.DB_PATH
    charlotte = {}
    if os.path.exists(charlotte_db):
        charlotte = place_ids_readonly(charlotte_db)
        overlap = sorted({r["id"] for r in records if r["id"] in charlotte})
        overlap_shown = sorted({r["id"] for r in records if r["id"] in charlotte and r["q"] == "true"})   # never print a not-qualified row's ID
        say(f"Charlotte database read-only: {len(charlotte)} leads; Place ID overlap with this pull: {len(overlap)}")
        if overlap:
            e_g = f" (e.g. {', '.join(overlap_shown[:3])})" if overlap_shown else ""
            say(f"STOP: {len(overlap)} Place ID(s) in this pull are already Charlotte leads{e_g}. "
                "Wrong city or wrong file. Nothing was written except this report.")
            return finish(2)
    else:
        say(f"WARNING: {charlotte_db} not found; Place ID and chain-name overlap with Charlotte NOT checked.")

    # ---- duplicate Place IDs must agree on qualified_lead ------------------------------------------------------
    by_pid = defaultdict(list)
    for rec in records:
        if rec["id"]:
            by_pid[rec["id"]].append(rec)
    disagree = [(pid_, recs) for pid_, recs in by_pid.items() if len({r["q"] for r in recs}) > 1]
    if disagree:
        disagree.sort(key=lambda t: t[1][0]["row"])
        say(f"STOP: {len(disagree)} Place ID(s) appear on more than one row with different qualified_lead values "
            "(blank versus false counts as different). Fix the pull; nothing was written except this report.")
        for pid_, recs in disagree[:3]:
            shown = pid_ if any(r["q"] == "true" for r in recs) else "(Place ID withheld: no row of it is qualified)"
            say(f"  {shown}: rows {', '.join(str(r['row']) for r in recs[:10])}")
        return finish(2)

    # ---- filters ---------------------------------------------------------------------------------------------
    # Counts run over ALL rows; a row is written to dropped_rows.csv only if it is qualified (the privacy rule).
    dropped = []                                       # (row, business_name, place_id, reason): qualified rows only
    kept, seen = [], {}
    n_missing = n_dup = 0
    for rec in records:
        qualified_row = rec["q"] == "true"
        if not rec["business_name"] or not rec["id"]:
            n_missing += 1
            if qualified_row:
                dropped.append((rec["row"], rec["business_name"], rec["id"], "missing business name or Place ID"))
        elif rec["id"] in seen:
            n_dup += 1
            if qualified_row:
                dropped.append((rec["row"], rec["business_name"], rec["id"], f"duplicate Place ID (first seen at row {seen[rec['id']]})"))
        else:
            seen[rec["id"]] = rec["row"]
            kept.append(rec)

    # the qualified filter: only qualified_lead = true goes on, before the address/state check, the skim and the draw
    qualified = [rec for rec in kept if rec["q"] == "true"]
    say("")
    say(f"raw pull: {len(records)} rows   workflow: {workflow}   pull date: {pull_date if pull_date else 'not given'}")
    say(f"removed as missing name/ID: {n_missing}   removed as duplicate Place ID: {n_dup}   remaining: {len(kept)}")
    say(f"qualified (true): {len(qualified)}   not qualified (false or blank): {len(kept) - len(qualified)}")
    say("")

    stage = []
    for rec in qualified:
        city, st = sdb.parse_city_state(rec["address"])
        rec["city"], rec["state"] = city, st
        if not city:
            dropped.append((rec["row"], rec["business_name"], rec["id"], f"address does not parse into city/state: {rec['address']!r}"))
        elif st.upper() != state.upper():
            dropped.append((rec["row"], rec["business_name"], rec["id"], f"state is {st}, not {state}"))
        else:
            stage.append(rec)
    n_addr = sum(1 for d in dropped if d[3].startswith("address"))
    n_state = sum(1 for d in dropped if d[3].startswith("state is"))

    population, skipped = [], []
    for rec in stage:
        reason = intake.skip_reason(rec["business_name"], rec["id"])
        (skipped if reason else population).append((rec, reason) if reason else rec)

    say(f"rows read {len(records)}, qualified (true) {len(qualified)} -> unparsable address {n_addr}, "
        f"not {state} {n_state}, intake skip {len(skipped)} -> filtered population {len(population)}")
    say(f"Place ID denylist: {len(intake.PLACE_ID_DENYLIST)} entr{'y' if len(intake.PLACE_ID_DENYLIST) == 1 else 'ies'}, all Charlotte "
        "Place IDs, so none can match here. Atlanta entries come from your skim: add each to PLACE_ID_DENYLIST in "
        "src/intake.py with a reason, in their own commit, then run this again with --sample.")
    for rec, reason in skipped:
        say(f"  intake skip: {rec['business_name']!r} -> {reason}")
    if not population:
        say("STOP: nothing is left after filtering.")
        return finish(2)

    # ---- flags + eyeball list --------------------------------------------------------------------
    ch_stems = defaultdict(list)
    for pid, nm in charlotte.items():
        ch_stems[hc.chain_stem(nm)].append(nm)
    name_ids = defaultdict(set)
    by_phone, by_street = defaultdict(set), defaultdict(set)
    for rec in population:
        name_ids[hc.name_key(rec["business_name"])].add(rec["id"])
        p = re.sub(r"\D", "", rec["phone"])[-10:]
        if p:
            by_phone[p].add(rec["business_name"])
        sk = _street_key(rec["address"])
        if sk:
            by_street[sk].add(rec["business_name"])
    for rec in population:
        flags = []
        stem = hc.chain_stem(rec["business_name"])
        if stem and stem in ch_stems:
            flags.append("chain_overlap:" + "|".join(ch_stems[stem][:2]))
        if len(name_ids[hc.name_key(rec["business_name"])]) > 1:
            flags.append("same_name_other_place_id")
        if not rec["phone"]:
            flags.append("no_phone")
        if not rec["website"]:
            flags.append("no_website")
        rec["flags"] = ";".join(flags)
        rec["eyeball"] = eyeball_reasons(rec, by_phone, by_street)

    flagged = [r for r in population if r["eyeball"]]
    chain = [r for r in population if "chain_overlap" in r["flags"]]
    sites = Counter(website_class(r["website"]) for r in population)
    cities = Counter(r["city"] for r in population)
    say(f"website mix: own domain {sites['own']}, booking platform {sites['booking']}, social {sites['social']}, blank {sites['blank']}")
    say(f"no phone: {sum(1 for r in population if not r['phone'])}   chain-name overlap with Charlotte (kept, flagged): {len(chain)}   "
        f"same name, different Place ID (kept, flagged): {sum(1 for r in population if 'same_name' in r['flags'])}")
    say("city mix (filtered population): " + ", ".join(f"{c} {n}" for c, n in cities.most_common()))
    say(f"EYEBALL LIST: {len(flagged)} of {len(population)} flagged for your skim -> eyeball_list.txt "
        "(flagged first, then everyone else; read ALL of it, no pattern catches every person-as-business record)")

    # ---- files -----------------------------------------------------------------------------------
    def write_csv(name, header_row, rows_out):
        with open(os.path.join(folder, name), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header_row)
            w.writerows(rows_out)

    def clean_row(r):
        return [r["business_name"], r["website"], r["address"], r["phone"], r["id"], r["google_url"], r["city"], r["state"], r["flags"]]

    write_csv("atlanta_leads_filtered.csv", hc.CLEAN_COLUMNS, [clean_row(r) for r in population])
    write_csv("dropped_rows.csv", ["source_row", "business_name", "place_id", "reason"], dropped)
    write_csv("skipped_preview.csv", ["place_id", "business_name", "website", "reason"],
              [[r["id"], r["business_name"], r["website"], why] for r, why in skipped])
    order = sorted(population, key=lambda r: (not r["eyeball"], r["business_name"].lower()))
    with open(os.path.join(folder, "eyeball_list.txt"), "w", encoding="utf-8") as f:
        f.write(f"EYEBALL LIST - {len(population)} leads in the filtered pull (nothing sampled yet unless noted)\n")
        f.write("Look for people, mobile or independent barbers, booth renters, non-shops. Denylist by Place ID.\n\n")
        f.write(f"=== FLAGGED ({len(flagged)}) ===\n")
        for i, r in enumerate(order, 1):
            if i == len(flagged) + 1:
                f.write(f"\n=== EVERYONE ELSE ({len(population) - len(flagged)}) ===\n")
            tag = "; ".join(r["eyeball"]) if r["eyeball"] else ""
            f.write(f"{i:3}. {r['business_name']} | {r['address']} | {r['phone']} | {r['website'] or '-'} | {r['id']}"
                    + (f"\n       -> {tag}" if tag else "") + (f"\n       flags: {r['flags']}" if r["flags"] else "") + "\n")
    say("wrote atlanta_leads_filtered.csv, dropped_rows.csv, skipped_preview.csv, eyeball_list.txt")

    # ---- the draw --------------------------------------------------------------------------------
    if sample is None:
        say("NOT the final list. Skim eyeball_list.txt, commit any denylist lines, then run again with "
            f"--sample {hc.SAMPLE_SIZE} --seed {hc.PROTOCOL_SEED} --skimmed.")
        say("No sample was drawn, so atlanta_leads_clean.csv was not written.")
        return finish(0)

    chosen = hc.draw_sample([r["id"] for r in population], sample, seed)
    by_id = {r["id"]: r for r in population}
    final = [by_id[p] for p in chosen]
    write_csv("atlanta_leads_clean.csv", hc.CLEAN_COLUMNS, [clean_row(r) for r in final])
    take_all = sample >= len(population)
    with open(os.path.join(folder, "sample.txt"), "w", encoding="utf-8") as f:
        f.write(f"seed {seed}\nrule: first {sample} Place IDs of the filtered population by sha256('<seed>:<place_id>') ascending"
                f"{' (population is not larger than the sample, so all are taken)' if take_all else ''}\n")
        f.write(f"population {len(population)}   drawn {len(final)}\n")
        f.write("city mix of the sample: " + ", ".join(f"{c} {n}" for c, n in Counter(r['city'] for r in final).most_common()) + "\n")
        f.write("place ids in draw order:\n" + "\n".join(chosen) + "\n")
    say(f"DRAWN: {len(final)} of {len(population)} (seed {seed}, after the skim)" + ("; population <= sample, all taken" if take_all else ""))
    say("city mix (sample): " + ", ".join(f"{c} {n}" for c, n in Counter(r['city'] for r in final).most_common()))
    say(f"chain-name overlap in the sample: {sum(1 for r in final if 'chain_overlap' in r['flags'])}")
    say("wrote atlanta_leads_clean.csv (seed this with setup_database.py --data-dir ... --csv) and sample.txt")
    return finish(0)


def iso_date(text):
    """argparse type: YYYY-MM-DD only (date.fromisoformat alone also takes 20261115 and 2026-W46-1 on newer Pythons)."""
    if not ISO_DATE_RE.match(text or ""):
        raise argparse.ArgumentTypeError(f"{text!r} is not a date in YYYY-MM-DD form")
    try:
        return datetime.date.fromisoformat(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{text!r} is not a real date ({e})")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("raw_csv", help="the pulled lead CSV (read, never modified)")
    ap.add_argument("--out", required=True, help="output folder, must be under <repo>/data/ (e.g. data/atlanta/intake)")
    ap.add_argument("--sample", type=int, default=None, help="draw this many leads from the filtered population")
    ap.add_argument("--seed", type=int, default=hc.PROTOCOL_SEED, help=f"draw seed (pre-registered: {hc.PROTOCOL_SEED})")
    ap.add_argument("--skimmed", action="store_true", help="I have read eyeball_list.txt and committed any denylist lines")
    ap.add_argument("--state", default="GA", help="two-letter state every lead must be in (default GA)")
    ap.add_argument("--charlotte-db", default=None, help="Charlotte database to check overlap against (default <repo>/leads.db)")
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW, help=f"n8n workflow that produced the pull, named in the report (default {DEFAULT_WORKFLOW})")
    ap.add_argument("--pull-date", required=True, type=iso_date, help="date the raw pull was saved, YYYY-MM-DD, named in the report")
    a = ap.parse_args(argv)
    if not os.path.exists(a.raw_csv):
        print(f"STOP: {a.raw_csv} not found")
        return 2
    return run(a.raw_csv, a.out, a.sample, a.seed, a.skimmed, a.charlotte_db, a.state, workflow=a.workflow, pull_date=a.pull_date.isoformat())


if __name__ == "__main__":
    sys.exit(main())
