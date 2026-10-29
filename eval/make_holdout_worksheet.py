"""Build the blind verification worksheet for the stage F holdout from the lead list ALONE.

    python eval/make_holdout_worksheet.py data/atlanta/intake/atlanta_leads_clean.csv \
        --out "data/atlanta/verify/Atlanta Verification Worksheet.xlsx"

Inputs: the clean lead CSV (atlanta_leads_clean.csv). Nothing else. This script never opens a database, the
pipeline's export or anything in data/atlanta/sealed/, and it imports nothing from the pipeline, so the
worksheet cannot contain an answer (eval/test_holdout_worksheet.py checks this).

The worksheet has one row per lead in a fixed random order (protocol section 3: sha256 of "<seed>:order:<place_id>"),
the lead's business name, address, phone and website, and EMPTY cells for the verdict, owner, co-owner, goes-by
name, source type, source URL, notes and minutes. Pipeline answers live in a different file you do not open
until you have finished.

It refuses to overwrite an existing worksheet (hours of verification could be lost). Delete the file yourself
if you really mean to start over. The output must be under <repo>/data/ (gitignored: it will hold real names).
"""
import argparse
import datetime
import os
import sys

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
import holdout_common as hc                       # noqa: E402
import paths                                      # noqa: E402

INSTRUCTIONS = [
    ("Stage F holdout: blind verification", None),
    ("Rules (eval/HOLDOUT_PROTOCOL.md sections 6 and 7 are the authority; this is the short version).", None),
    ("", None),
    ("BLIND", "Do not open, preview or read anything in data/atlanta/sealed/, and do not look at any pipeline output, until EVERY row below has a verdict and this file is saved. Do not let an assistant read that folder either."),
    ("TIME BOX", "8 minutes a lead. Write the minutes in the `minutes` column. If 8 minutes pass without a determination, the verdict is `Not determined`. You may split verification across sessions; save between them."),
    ("", None),
    ("VERDICTS (column `verdict`, one per row)", None),
    (hc.VERDICT_OWNER, "A source names an owner. Fill owner_first and owner_last (last may be empty only if no source gives it), optionally goes_by, and co_owner_first / co_owner_last if a source names co-owners. Fill source_type and source_url."),
    (hc.VERDICT_NO_OWNER, "Allowed ONLY after you checked every source type below. Set `checked` to Yes. This is a claim that the owner is not publicly findable. Any name the pipeline later gives for this shop will count as wrong."),
    (hc.VERDICT_AMBIGUOUS, "Sources conflict, or the business cannot be told apart from another of the same name. The row leaves every denominator."),
    (hc.VERDICT_UNDETERMINED, "The 8 minutes ended without a determination. The row leaves every denominator. If more than 20% of rows end Ambiguous or Not determined, the result will say so."),
    ("", None),
    ("SOURCE TYPES (columns `source_type`, `source_type_2`)", None),
    ("LinkedIn", "Search-result snippets only. Never fetch or scrape a LinkedIn page."),
    ("News", "A news article or local-press feature."),
    ("BBB", "A Better Business Bureau profile."),
    ("Booksy", "A Booksy page. The Booksy 'owner' is whoever holds the subscription, not proof of ownership."),
    ("Video", "YouTube, TikTok video or any video where the owner is named or says so."),
    ("Directory", "A business directory or listing site (Yelp, Manta, city-data and similar)."),
    ("Own website", "The shop's own site (about page, footer, booking page on its own domain)."),
    ("Social (IG/FB)", "The shop's Instagram or Facebook page or bio."),
    ("GA registry", "Georgia Secretary of State business lookup, BY HAND only. Never script it."),
    ("Other", "Anything else; say what in `notes`."),
    ("", None),
    ("EVIDENCE RULES", "A stylist or head-barber title, reviews and testimonials are NOT evidence of ownership. Record the main source's type in source_type and URL in source_url; a second source is optional."),
    ("CO-OWNERS", "If sources name two owners, put the second in co_owner_first / co_owner_last. A pipeline answer naming either will be scored correct."),
    ("GOES-BY", "A nickname or shop name the owner uses (for example 'Donny Blendz'). A pipeline answer using it is scored as a partial match."),
]


def build(clean_csv, out_path, seed=hc.PROTOCOL_SEED):
    """Write the worksheet. Returns the number of rows. Raises ValueError for a bad input or an existing output."""
    rows = hc.read_clean_csv(clean_csv)
    if not rows:
        raise ValueError("the lead list is empty")
    for need in ("business_name", "id"):
        if need not in rows[0]:
            raise ValueError(f"the lead list has no '{need}' column")
    ids = [r["id"] for r in rows]
    if any(not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("the lead list has a missing or duplicate Place ID")
    parent = os.path.dirname(os.path.abspath(out_path))
    paths.resolve_data_dir(parent)                    # the output must live under <repo>/data/ (raises ValueError)
    if os.path.exists(out_path):
        raise ValueError(f"{out_path} already exists. It may hold hours of verification, so it is never overwritten. "
                         "Move or delete it yourself to start over.")
    os.makedirs(parent, exist_ok=True)

    ordered = sorted(rows, key=lambda r: (hc.order_key(seed, r["id"]), r["id"]))
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Verify"
    lists = wb.create_sheet("Lists")
    guide = wb.create_sheet("Instructions", 0)

    head_fill = PatternFill("solid", fgColor="DDDDDD")
    hand_fill = PatternFill("solid", fgColor="FFF4CC")
    ws.append(hc.VERIFY_COLUMNS)
    for j, name in enumerate(hc.VERIFY_COLUMNS, 1):
        cell = ws.cell(row=1, column=j)
        cell.font = Font(bold=True)
        cell.fill = hand_fill if j - 1 >= hc.FIRST_HAND_COLUMN else head_fill
    for i, r in enumerate(ordered, 1):
        ws.append([i, r["id"], r["business_name"], r.get("address", ""), r.get("phone", ""), r.get("website", ""),
                   r.get("google_url", "")] + [None] * (len(hc.VERIFY_COLUMNS) - hc.FIRST_HAND_COLUMN))
    last = len(ordered) + 1

    for j, w in enumerate([5, 14, 34, 40, 16, 34, 14, 17, 14, 14, 14, 14, 14, 16, 40, 16, 40, 9, 9, 50], 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(j)].width = w
    ws.freeze_panes = "D2"
    for row in ws.iter_rows(min_row=2, max_row=last):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=cell.column_letter in ("C", "D", "F", "T"))

    lists.append(["verdicts", "source_types", "checked"])
    for i in range(max(len(hc.VERDICTS), len(hc.SOURCE_TYPES), len(hc.CHECKED_VALUES))):
        lists.append([hc.VERDICTS[i] if i < len(hc.VERDICTS) else None,
                      hc.SOURCE_TYPES[i] if i < len(hc.SOURCE_TYPES) else None,
                      hc.CHECKED_VALUES[i] if i < len(hc.CHECKED_VALUES) else None])

    def add_list(col_name, src_col, n):
        col = openpyxl.utils.get_column_letter(hc.VERIFY_COLUMNS.index(col_name) + 1)
        dv = DataValidation(type="list", formula1=f"=Lists!${src_col}$2:${src_col}${n + 1}", allow_blank=True,
                            showErrorMessage=True, errorTitle="Not in the list", error=f"Pick a value from the list ({col_name}).")
        ws.add_data_validation(dv)
        dv.add(f"{col}2:{col}{last}")

    add_list("verdict", "A", len(hc.VERDICTS))
    add_list("source_type", "B", len(hc.SOURCE_TYPES))
    add_list("source_type_2", "B", len(hc.SOURCE_TYPES))
    add_list("checked", "C", len(hc.CHECKED_VALUES))

    for text, detail in INSTRUCTIONS:
        guide.append([text, detail])
    guide["A1"].font = Font(bold=True, size=14)
    for row in guide.iter_rows(min_row=3):
        row[0].font = Font(bold=True)
        row[1].alignment = Alignment(wrap_text=True, vertical="top")
        row[0].alignment = Alignment(vertical="top", wrap_text=True)
    guide.column_dimensions["A"].width = 38
    guide.column_dimensions["B"].width = 110
    guide.append([])
    guide.append(["Built from", f"lead list sha256 {hc.file_sha256(clean_csv)[:16]}..., {len(ordered)} leads, order seed {seed}, "
                                f"{datetime.date.today().isoformat()}. The lead list holds no pipeline output."])
    wb.save(out_path)
    return len(ordered)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clean_csv", help="atlanta_leads_clean.csv from eval/holdout_intake.py --sample")
    ap.add_argument("--out", required=True, help="worksheet path, under <repo>/data/ (e.g. data/atlanta/verify/...xlsx)")
    ap.add_argument("--seed", type=int, default=hc.PROTOCOL_SEED, help=f"row-order seed (pre-registered: {hc.PROTOCOL_SEED})")
    a = ap.parse_args(argv)
    try:
        n = build(a.clean_csv, a.out, a.seed)
    except (ValueError, OSError) as e:
        print(f"STOP: {e}")
        return 2
    print(f"Wrote {a.out}: {n} leads, empty verdict/owner/source cells. Fill it in blind; do not open data/atlanta/sealed/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
