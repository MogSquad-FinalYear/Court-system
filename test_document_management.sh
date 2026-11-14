#!/usr/bin/env bash
# End-to-end test of the Document Management feature.
#
#   ./test_document_management.sh
#
# Needs the server running:
#   ./.venv/bin/python -m uvicorn app.main:app --port 8020
#
# NOTE: step 2 calls the live Landing AI API (parse + extract) and spends
# credits. Everything after it is local. Pass --no-upload to reuse the most
# recently uploaded document instead of uploading a new one.
set -u

BASE="${BASE:-http://127.0.0.1:8020}"
PY=./.venv/bin/python3

# Principals, as "user:role:jurisdiction"
CLERK="-H X-User-Id:clerk-01   -H X-Role:clerk     -H X-Jurisdiction:KA-BLR"
JUDGE="-H X-User-Id:judge-07   -H X-Role:judge     -H X-Jurisdiction:KA-BLR"
ADVOCATE="-H X-User-Id:adv-vinay -H X-Role:advocate -H X-Jurisdiction:KA-BLR"
PUBLIC="-H X-User-Id:anon-1    -H X-Role:public    -H X-Jurisdiction:KA-BLR"
OTHER_CLERK="-H X-User-Id:clerk-99 -H X-Role:clerk  -H X-Jurisdiction:KA-MYS"
OTHER_REG="-H X-User-Id:reg-99   -H X-Role:registrar -H X-Jurisdiction:KA-MYS"

hr() { printf '\n\033[1m%s\033[0m\n' "$1"; }
code() { curl -s -o /dev/null -w "%{http_code}" "$@"; }

hr "0. Health — which features actually exist"
curl -s "$BASE/health" | $PY -m json.tool

# --------------------------------------------------------------------
hr "1. Make a test court filing (PDF)"
$PY - <<'PYEOF'
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
p = "/tmp/cms_petition.pdf"
c = canvas.Canvas(p, pagesize=A4); w, h = A4; y = h - 70
def L(t, dy=18, f=("Helvetica", 11)):
    global y; c.setFont(*f); c.drawString(60, y, t); y -= dy
L("IN THE HIGH COURT OF KARNATAKA AT BENGALURU", 26, ("Helvetica-Bold", 13))
L("CRL.P. No. 4471/2025", 24, ("Helvetica-Bold", 12))
L("Petition under Section 482 of the Code of Criminal Procedure, 1973", 26, ("Helvetica-Bold", 11))
L("BETWEEN:", 20, ("Helvetica-Bold", 11))
L("1. Ramesh Kumar S/o Lakshmana, aged 41 years,")
L("   resident of Jayanagar, Bengaluru .......... PETITIONER")
L(""); L("AND:", 20, ("Helvetica-Bold", 11))
L("1. State of Karnataka, by Jayanagar Police Station")
L("2. Smt. Anitha Rao W/o Prakash .......... RESPONDENTS")
L(""); L("Filed on: 14 March 2025", 22, ("Helvetica-Bold", 11))
L("Next listed on: 02 April 2025", 24, ("Helvetica-Bold", 11))
L("PRAYER:", 20, ("Helvetica-Bold", 11))
L("The petitioner prays that this Hon'ble Court be pleased to quash the")
L("proceedings in C.C. No. 221/2025 pending before the XIV ACMM,")
L("Bengaluru, registered for offences punishable under Sections 420 and")
L("406 of the Indian Penal Code, 1860, and under Section 66D of the")
L("Information Technology Act, 2000.")
L(""); L("Advocate for the Petitioner: Sri. Vinay Hegde", 18, ("Helvetica-Bold", 11))
c.showPage(); c.save(); print(f"  wrote {p}")
PYEOF

# --------------------------------------------------------------------
if [ "${1:-}" = "--no-upload" ]; then
  hr "2. SKIPPED upload (--no-upload) — reusing latest document"
  DOC=$($PY -c "
import sqlite3; c=sqlite3.connect('data/court.db')
print(c.execute('select id from documents order by created_at desc limit 1').fetchone()[0])")
else
  hr "2. UPLOAD as clerk  →  real Landing AI parse + extract  (spends credits)"
  curl -s -X POST "$BASE/api/documents" $CLERK \
    -F "file=@/tmp/cms_petition.pdf" \
    -F "title=Quash petition - Ramesh Kumar" \
    -F "jurisdiction=KA-BLR" \
    -F "case_number=CRL.P. 4471/2025" \
    -F "doc_type=petition" \
    -F "classification=restricted" > /tmp/cms_up.json
  DOC=$($PY -c "
import json; d=json.load(open('/tmp/cms_up.json')); print(d['document_id'])" 2>/dev/null)
  if [ -z "${DOC:-}" ]; then
    echo "  UPLOAD FAILED:"; head -c 400 /tmp/cms_up.json; echo; exit 1
  fi
  $PY - <<'PYEOF'
import json
d = json.load(open('/tmp/cms_up.json')); v = d['versions'][0]
print(f"  document_id : {d['document_id']}")
print(f"  version     : v{v['version_no']}  status={v['extraction_status']}  "
      f"pages={v['page_count']}  elements={v['element_count']}")
print(f"  sha256      : {v['content_hash'][:24]}…")
print("  FIELDS LANDING AI EXTRACTED:")
for k, val in (v.get('fields') or {}).items():
    print(f"    {k:16} = {val}")
PYEOF
fi
echo "  DOC=$DOC"

# --------------------------------------------------------------------
hr "3. ACCESS CONTROL — same document, different principals"
printf "  judge      in-jurisdiction  -> HTTP %s  (expect 200)\n" "$(code "$BASE/api/documents/$DOC" $JUDGE)"
printf "  advocate   in-jurisdiction  -> HTTP %s  (expect 200)\n" "$(code "$BASE/api/documents/$DOC" $ADVOCATE)"
printf "  public     in-jurisdiction  -> HTTP %s  (expect 404, not 403)\n" "$(code "$BASE/api/documents/$DOC" $PUBLIC)"
printf "  clerk      OTHER district   -> HTTP %s  (expect 404)\n" "$(code "$BASE/api/documents/$DOC" $OTHER_CLERK)"
printf "  registrar  OTHER district   -> HTTP %s  (expect 200, cross-jurisdiction)\n" "$(code "$BASE/api/documents/$DOC" $OTHER_REG)"

# --------------------------------------------------------------------
hr "4. SEARCH — full-text over the Markdown Landing AI produced"
echo "  query: 'quash proceedings'"
curl -s "$BASE/api/documents/search?q=quash+proceedings" $JUDGE | $PY -c "
import json,sys
hits=json.load(sys.stdin)
print(f'    {len(hits)} hit(s)')
for h in hits:
    print(f\"    · {h['title']} v{h['version_no']}  [{h['case_number']}]  doc={h['document_id'][:8]}…\")
    print(f\"      {h['excerpt'][:100]}\")"
echo "  query: 'maritime salvage'  (should miss)"
curl -s "$BASE/api/documents/search?q=maritime+salvage" $JUDGE | $PY -c "
import json,sys; print(f'    {len(json.load(sys.stdin))} hit(s)')"
echo "  same query as public (must not leak a restricted doc):"
curl -s "$BASE/api/documents/search?q=quash+proceedings" $PUBLIC | $PY -c "
import json,sys; print(f'    {len(json.load(sys.stdin))} hit(s)')"

# --------------------------------------------------------------------
hr "5. EXTRACTED TEXT"
curl -s "$BASE/api/documents/$DOC/text" $JUDGE | $PY -c "
import json,sys; d=json.load(sys.stdin)
print(f\"  status={d['extraction_status']}\")
print('  --- markdown (first 320 chars) ---')
print('  ' + (d['markdown'] or '')[:320].replace(chr(10), chr(10)+'  '))"

# --------------------------------------------------------------------
hr "6. SIGNATURES & SEALS — role gates"
printf "  clerk    sign -> HTTP %s  (expect 403)\n" \
  "$(code -X POST "$BASE/api/documents/$DOC/versions/1/sign" $CLERK)"
printf "  advocate seal -> HTTP %s  (expect 403)\n" \
  "$(code -X POST "$BASE/api/documents/$DOC/versions/1/seal" $ADVOCATE)"
echo "  advocate sign:"
curl -s -X POST "$BASE/api/documents/$DOC/versions/1/sign" $ADVOCATE \
  -F "reason=Filed on behalf of petitioner" | $PY -c "
import json,sys; d=json.load(sys.stdin)
print(f\"    kind={d['kind']} valid={d['valid']} signature={d['signature'][:20]}…\")"
echo "  judge seal:"
curl -s -X POST "$BASE/api/documents/$DOC/versions/1/seal" $JUDGE \
  -F "reason=Admitted for hearing" | $PY -c "
import json,sys; d=json.load(sys.stdin)
print(f\"    seal_number={d['seal_number']} court={d['court_id']} valid={d['valid']}\")"

# --------------------------------------------------------------------
hr "7. INTEGRITY — clean"
curl -s "$BASE/api/documents/$DOC/integrity" $JUDGE | $PY -c "
import json,sys; d=json.load(sys.stdin)
print(f\"  intact={d['intact']}\")
for v in d['versions']:
    print(f\"  v{v['version_no']} bytes_match_hash={v['bytes_match_hash']} \"
          f\"attestations={len(v['attestations'])}\")"

hr "8. TAMPER TEST — append one byte to the stored blob"
H=$($PY -c "
import sqlite3; c=sqlite3.connect('data/court.db')
print(c.execute('select content_hash from document_versions where document_id=? and version_no=1',('$DOC',)).fetchone()[0])")
BLOB="data/blobs/${H:0:2}/${H:2:2}/$H"
printf 'X' >> "$BLOB"
echo "  tampered: $BLOB"
curl -s "$BASE/api/documents/$DOC/integrity" $JUDGE | $PY -c "
import json,sys; d=json.load(sys.stdin)
print(f\"  intact={d['intact']}   <- expect False\")
for v in d['versions']:
    if v['version_no']!=1: continue
    print(f\"  v1 bytes_match_hash={v['bytes_match_hash']}\")
    for a in v['attestations']:
        print(f\"    {a['kind']:9} valid={a['valid']} sig_ok={a['signature_valid']} \"
              f\"blob_verified={a['blob_verified']}\")
    print('    ^ signature math still fine, but the bytes changed underneath it')"
truncate -s -1 "$BLOB"
echo "  byte removed — re-check:"
curl -s "$BASE/api/documents/$DOC/integrity" $JUDGE | $PY -c "
import json,sys; print(f\"  intact={json.load(sys.stdin)['intact']}\")"

# --------------------------------------------------------------------
hr "8b. VERSIONING — supersede v1, prior versions stay intact"
echo "  re-upload the stored bytes of the latest version (must be refused):"
# Fetched from the API rather than regenerated: reportlab stamps a creation
# time into the PDF, so a regenerated file is never byte-identical.
curl -s "$BASE/api/documents/$DOC/content" $JUDGE -o /tmp/cms_latest.pdf
printf "    HTTP %s  (expect 409, nothing to supersede)\n" \
  "$(code -X POST "$BASE/api/documents/$DOC/versions" $CLERK -F "file=@/tmp/cms_latest.pdf")"
$PY - <<'PYEOF'
# A corrected filing: same petition, one line changed.
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
c = canvas.Canvas("/tmp/cms_petition_v2.pdf", pagesize=A4); w, h = A4
c.setFont("Helvetica-Bold", 13); c.drawString(60, h - 70, "IN THE HIGH COURT OF KARNATAKA AT BENGALURU")
c.setFont("Helvetica-Bold", 12); c.drawString(60, h - 96, "CRL.P. No. 4471/2025  (AMENDED)")
c.setFont("Helvetica", 11); c.drawString(60, h - 126, "Amended to correct the respondent's address for service.")
c.showPage(); c.save()
PYEOF
echo "  upload the amended petition as v2:"
curl -s -X POST "$BASE/api/documents/$DOC/versions" $CLERK \
  -F "file=@/tmp/cms_petition_v2.pdf" \
  -F "note=Amended to correct respondent address" | $PY -c "
import json,sys; d=json.load(sys.stdin)
for v in sorted(d['versions'], key=lambda v: v['version_no']):
    atts = v.get('attestations') or []
    kinds = ','.join(a['kind'] for a in atts) or 'none'
    print(f\"    v{v['version_no']}  status={v['extraction_status']:9} \"
          f\"hash={v['content_hash'][:12]}…  attestations={len(atts)} ({kinds})\")
newest = max(v['version_no'] for v in d['versions'])
print(f'    ^ v1 keeps its signature and seal; v{newest} arrives unsigned')"
printf "  content?version=1 -> HTTP %s   content?version=2 -> HTTP %s  (both still fetchable)\n" \
  "$(code "$BASE/api/documents/$DOC/content?version=1" $JUDGE)" \
  "$(code "$BASE/api/documents/$DOC/content?version=2" $JUDGE)"

hr "9. AUDIT TRAIL — judge/registrar only, refusals logged too"
printf "  clerk  -> HTTP %s  (expect 403)\n" "$(code "$BASE/api/documents/$DOC/audit" $CLERK)"
curl -s "$BASE/api/documents/$DOC/audit?limit=8" $JUDGE | $PY -c "
import json,sys; d=json.load(sys.stdin)
print(f\"  judge  -> {len(d['entries'])} entries (most recent first)\")
for e in d['entries']:
    flag = 'ALLOWED' if e['allowed'] else 'REFUSED'
    print(f\"    {flag:8} {e['actor_role']:10} {e['action']:12} {e['detail'] or ''}\")"

# --------------------------------------------------------------------
hr "10. THE OTHER THREE FEATURES — live, not stubs"
echo "  each endpoint below is reachable; test_end_to_end.sh exercises them properly"
printf "  %-4s %-36s -> HTTP %s\n" "GET" "/api/filing/case-types" \
  "$(code "$BASE/api/filing/case-types" $CLERK)"
printf "  %-4s %-36s -> HTTP %s\n" "GET" "/api/scheduling/resources" \
  "$(code "$BASE/api/scheduling/resources" $CLERK)"
printf "  %-4s %-36s -> HTTP %s\n" "GET" "/api/integrations" \
  "$(code "$BASE/api/integrations" $CLERK)"
echo "  connector registry (all adapters are mocks):"
curl -s "$BASE/api/integrations" $CLERK | $PY -c "
import json,sys
for c in json.load(sys.stdin)['connectors']:
    print(f\"    {c['connector_id']:14} {c['implementation']:6} circuit={c['circuit']}\")"

hr "DONE"
