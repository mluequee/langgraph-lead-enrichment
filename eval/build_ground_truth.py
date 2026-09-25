"""Build eval/data/ground_truth.json from the hand-verified audit worksheet.

Run once, and again only if the worksheet changes:
    python eval/build_ground_truth.py

Reads the worksheet from the repo root by default
("Owner Verification Worksheet.xlsx"). Pass a path to use a different file:
    python eval/build_ground_truth.py "path/to/other.xlsx"

The worksheet's own Scoring tab is NOT used. It scores verdicts raw and reports
LangGraph at ~30%. The four corrections below are what produced the recorded
audit result (LangGraph 17/41, n8n 18/27). Each one is sourced.

Output contains real people's names. eval/data/ is gitignored; keep it that way.
"""
import json
import os
import sys

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scoring import match_person, norm_tokens  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_XLSX = os.path.join(os.path.dirname(HERE), "Owner Verification Worksheet.xlsx")
OUT = os.path.join(HERE, "data", "ground_truth.json")

# --- Corrections applied at scoring time in the 2026-09-21 audit -------------
CORRECTIONS = {
    # Both systems named the same person; verdict credited only one side.
    "Gordons' Historic Barbershop": ("Both correct", "Both systems named Michael Gordon"),
    "Olde Charlotte Barbershop": ("Both correct", "Both systems named Lee Millsaps"),
    # Marvin's note: LinkedIn lists both Niki and Victoria as owners.
    "Major Barbershop": ("Both correct", "Co-owners Niki and Victoria (LinkedIn)"),
    # Marvin's note: no source names an owner; only one candidate existed.
    "J&C Barber Room": ("No owner public", "Verdict 'B correct' on a one-candidate row; note says none found"),
}

# --- Co-owners named in Marvin's notes. A pipeline answer naming one of these
# --- counts as correct. Delete any line you disagree with. --------------------
CO_OWNERS = {
    "Major Barbershop": [("Victoria", "Voncannon")],     # the scored correction above
    "Blendsorial Barbershop": [("Eric", "Cheek")],       # note: "one of them are Eric Cheek"
    "Doyenne": [("Alicia", "Phillips")],                  # note: co-owner barbershop
    "La Barberia Barbershop": [("Camila", "Alves")],      # note: wife and co-owner
    "The Barber Lounge": [("Spring", "Mumpower")],        # note: Ron & Spring named owners
    "The Hair Cave": [("Leyli", "Mamedov")],              # note: owners Ruslam and Leyli
    "Knights of the Razor | Exclusive": [("Damian", "Johnson")],  # note: brother duo
}

# --- Goes-by names from Marvin's notes (the audit's alias list) -------------
NOTE_ALIASES = {
    "4 gentleman haircuts": ["Jay"],
    "Jody hawk @ Landrys Barbershop": ["Jody Hawk", "Jody"],
    "Lucky Spot Barbershop Charlotte (Inside Walmart)": ["Lucky"],
    "MODERN CLASSICS (VILLA HEIGHTS)": ["Donny Blendz"],
    "Barber Kons the Butcher": ["Kons"],
}


def key(name):
    """Stable lookup key for a business name (whitespace, case, punctuation)."""
    return " ".join(t for t in norm_tokens(name) if not t.isdigit())


CORRECTIONS = {key(k): v for k, v in CORRECTIONS.items()}
CO_OWNERS = {key(k): v for k, v in CO_OWNERS.items()}
NOTE_ALIASES = {key(k): v for k, v in NOTE_ALIASES.items()}


def cell(v):
    if v is None:
        return ""
    s = str(v).strip()
    # Worksheet uses "n/a" in some name cells (e.g. Wize has no known surname).
    return "" if s.lower() in ("nan", "n/a", "na", "none", "unknown") else s


def read_sheet(ws, header_row):
    headers = [cell(c.value) for c in ws[header_row]]
    rows = []
    for r in ws.iter_rows(min_row=header_row + 1, values_only=True):
        if r[0] is None:
            continue
        rows.append(dict(zip(headers, [cell(v) for v in r])))
    return rows


def build(xlsx_path):
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ver = read_sheet(wb["Verification"], 2)
    blind = {r["#"]: r for r in read_sheet(wb["Blind Key"], 2)}

    out, applied = [], []
    for r in ver:
        biz = r["Business Name"]
        k = key(biz)
        verdict = r["Verdict"]
        note = ""
        if k in CORRECTIONS:
            verdict, note = CORRECTIONS[k]
            applied.append(biz)
        if "Both agreed" in r["Row Type"] and verdict in ("A correct", "B correct"):
            raise SystemExit(f"Uncorrected agreed row: {biz} - add it to CORRECTIONS")

        if verdict == "No owner public":
            kind = "no_owner"
        elif verdict == "Ambiguous":
            kind = "ambiguous"
        else:
            kind = "owner"
            if not r["True First"]:
                raise SystemExit(f"Owner row with no True First: {biz}")

        owners = []
        if kind == "owner":
            owners.append({"first": r["True First"], "last": r["True Last"] or None,
                           "aliases": list(NOTE_ALIASES.get(k, []))})
            for f, l in CO_OWNERS.get(k, []):
                owners.append({"first": f, "last": l, "aliases": []})

        # Historical 08-25 outputs, and whether the corrected verdict credited them.
        bk = blind[r["#"]]
        partial = "Partial" in r["Row Type"]
        hist = {}
        for system in ("LangGraph", "n8n"):
            said = bk.get(f"{system} said", "")
            if partial or said.startswith("see phase7"):
                hist[system] = None                       # withheld in the audit
                continue
            letter = "A" if bk["A is"] == system else ("B" if bk["B is"] == system else None)
            credited = bool(said) and verdict in (f"{letter} correct", "Both correct")
            hist[system] = {"said": said, "credited": credited}
            # A credited answer the matcher can't see (e.g. "Danny" for Daniel)
            # becomes an alias, so the scorer reproduces the hand grading.
            if credited and owners and not any(match_person(said, "", p) for p in owners):
                owners[0]["aliases"].append(said)

        out.append({
            "id": int(float(r["#"])), "business_name": biz, "key": k,
            "row_type": r["Row Type"], "subset": "partial" if partial else "audit",
            "kind": kind, "owners": owners,
            "verdict_original": r["Verdict"], "verdict_scored": verdict,
            "correction": note, "historical": hist,
        })

    for table, name in ((CO_OWNERS, "CO_OWNERS"), (NOTE_ALIASES, "NOTE_ALIASES")):
        unmatched = set(table) - {key(r["Business Name"]) for r in ver}
        if unmatched:
            raise SystemExit(f"{name} entries that matched no row: {unmatched}")
    missing = set(CORRECTIONS) - {key(b) for b in applied}
    if missing:
        raise SystemExit(f"Corrections that matched no row: {missing}")
    return out, applied


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_XLSX
    rows, applied = build(path)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)
    kinds = {k: sum(r["kind"] == k for r in rows) for k in ("owner", "no_owner", "ambiguous")}
    print(f"Wrote {len(rows)} rows to {OUT}")
    print(f"  audit rows {sum(r['subset'] == 'audit' for r in rows)}, "
          f"partial rows {sum(r['subset'] == 'partial' for r in rows)}")
    print(f"  kinds {kinds}")
    print(f"  corrections applied ({len(applied)}): {', '.join(applied)}")
