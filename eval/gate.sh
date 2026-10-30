#!/usr/bin/env bash
# Stage F gate. Free and offline: runs every eval/test_*.py with dummy credentials (the tests set them), then checks
#   - each test file exits 0 and prints no FAIL line, and the total number of passing checks;
#   - the scorer reproduces the hand audit ("Scorer trusted.");
#   - the frozen pipeline files (eval/frozen_check.py): graph, reconcile, gemini_path and evidence identical to
#     stage-e-done, intake.py differing only by added PLACE_ID_DENYLIST entries; plus a raw git diff on the four;
#   - the Charlotte leads.db still has the recorded SHA-256, and its pre-stage-F backup exists.
#
#   bash eval/gate.sh                   # from anywhere inside the repository; exit code 0 = PASS, 1 = FAIL
#
# Overrides (environment): PYTHON (interpreter, default venv/Scripts/python.exe or venv/bin/python),
# CHARLOTTE_DB_SHA256 (expected hash of leads.db).
# Needs bash, git, sha256sum and grep (Git for Windows provides all four). Contains no credentials.

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 2
if [ -n "$PYTHON" ]; then PY="$PYTHON"
elif [ -x venv/Scripts/python.exe ]; then PY=venv/Scripts/python.exe
elif [ -x venv/bin/python ]; then PY=venv/bin/python
else PY=python; fi

EXPECT="${CHARLOTTE_DB_SHA256:-75af9f7830e2702b739d47f4d2e7a6abed04789d9ad04be4a62423e4aebf3ab9}"
fail=0
total=0

for t in eval/test_*.py; do
  out=$($PY -u "$t" 2>&1); rc=$?
  p=$(printf '%s\n' "$out" | grep -c '^PASS'); f=$(printf '%s\n' "$out" | grep -c '^FAIL')
  total=$((total + p))
  echo "$t rc=$rc PASS=$p FAIL=$f"
  if [ $rc -ne 0 ] || [ "$f" -ne 0 ]; then fail=1; printf '%s\n' "$out" | tail -15; fi
done
echo "total passing checks: $total"

$PY -u eval/test_scoring.py 2>&1 | grep -q "Scorer trusted." && echo "scorer: Scorer trusted." || { echo "scorer: NOT TRUSTED"; fail=1; }

fz=$($PY eval/frozen_check.py 2>&1); frc=$?
echo "$fz"; [ $frc -ne 0 ] && fail=1
# belt and braces: the four fully frozen files must also show an empty raw git diff
d=$(git diff stage-e-done -- src/graph.py src/reconcile.py src/gemini_path.py src/evidence.py | wc -l)
echo "raw git diff lines, four frozen files: $d"; [ "$d" -ne 0 ] && fail=1

h=$(sha256sum leads.db | cut -d' ' -f1)
[ "$h" = "$EXPECT" ] && echo "leads.db hash OK" || { echo "leads.db HASH CHANGED: $h"; fail=1; }
[ -f leads_pre_stage_f_backup.db ] && echo "backup present" || { echo "backup MISSING"; fail=1; }

echo "GATE: $([ $fail -eq 0 ] && echo PASS || echo FAIL)"
exit $fail
