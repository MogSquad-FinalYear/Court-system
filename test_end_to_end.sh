#!/usr/bin/env bash
# Full pipeline test across all four features.
#
# A real matter, start to finish:
#   file a petition -> scrutiny rejects it -> fix it -> accepted with a case
#   number -> schedule a hearing against real conflicts -> cause list ->
#   reminders -> pay the fee through the Integration Hub.
#
# Needs the server running on :8020. Step 2 calls the live Landing AI API.
set -u
BASE="${BASE:-http://127.0.0.1:8020}"
PY=./.venv/bin/python3

CLERK="-H X-User-Id:clerk-01 -H X-Role:clerk -H X-Jurisdiction:KA-BLR"
JUDGE="-H X-User-Id:judge-07 -H X-Role:judge -H X-Jurisdiction:KA-BLR"
REG="-H X-User-Id:reg-01 -H X-Role:registrar -H X-Jurisdiction:KA-BLR"
ADV="-H X-User-Id:adv-vinay -H X-Role:advocate -H X-Jurisdiction:KA-BLR"
JSON="-H Content-Type:application/json"

hr(){ printf '\n\033[1m── %s\033[0m\n' "$1"; }
ok(){ printf '   \033[32m✓\033[0m %s\n' "$1"; }
code(){ curl -s -o /dev/null -w "%{http_code}" "$@"; }

hr "0  Health"
curl -s "$BASE/health" | $PY -m json.tool | sed 's/^/   /'

hr "1  Seed court resources"
$PY seed.py | sed 's/^/   /'

hr "2  Upload the petition (Document Management + Landing AI)"
$PY - <<'PYEOF'
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
c=canvas.Canvas("/tmp/e2e_petition.pdf",pagesize=A4); w,h=A4; y=h-70
def L(t,dy=18,f=("Helvetica",11)):
    global y; c.setFont(*f); c.drawString(60,y,t); y-=dy
L("IN THE HIGH COURT OF KARNATAKA AT BENGALURU",26,("Helvetica-Bold",13))
L("Criminal Appeal",24,("Helvetica-Bold",12))
L("Memorandum of Appeal under Section 374 CrPC",26,("Helvetica-Bold",11))
L("Appellant: Ramesh Kumar S/o Lakshmana")
L("Respondent: State of Karnataka")
L("Impugned judgment dated: 01 October 2026",22,("Helvetica-Bold",11))
L("Advocate for the Appellant: Sri. Vinay Hegde",22,("Helvetica-Bold",11))
L("GROUNDS:",20,("Helvetica-Bold",11))
L("The conviction under Section 420 of the Indian Penal Code, 1860 rests")
L("solely on uncorroborated testimony and ought to be set aside.")
c.showPage(); c.save(); print("   built /tmp/e2e_petition.pdf")
PYEOF

for role in memorandum_of_appeal certified_copy_of_judgment vakalatnama; do
  rid=$(curl -s -X POST "$BASE/api/documents" $CLERK \
    -F "file=@/tmp/e2e_petition.pdf" -F "title=$role" \
    -F "jurisdiction=KA-BLR" -F "doc_type=$role" -F "classification=restricted" \
    | $PY -c "import json,sys;print(json.load(sys.stdin)['document_id'])")
  echo "   $role -> $rid"
  eval "DOC_${role}=$rid"
done
ok "three documents stored, extracted and indexed"

hr "3  Fee calculation (placeholder schedule, versioned by date)"
curl -s -X POST "$BASE/api/filing/fees/calculate" $CLERK $JSON \
  -d '{"case_type":"CRL.A","filing_date":"2026-11-13"}' \
  | $PY -c "
import json,sys; d=json.load(sys.stdin)
print(f\"   total={d['total']} {d['currency']}  (schedule from {d['schedule_effective_from']})\")
for l in d['lines']: print(f\"     {l['label']:46} {l['amount']:>10.2f}\")"
echo "   -- same filing, dated before the 2024 revision:"
curl -s -X POST "$BASE/api/filing/fees/calculate" $CLERK $JSON \
  -d '{"case_type":"CRL.A","filing_date":"2021-06-01"}' \
  | $PY -c "
import json,sys; d=json.load(sys.stdin)
print(f\"   total={d['total']} (schedule from {d['schedule_effective_from']}) <- older, cheaper\")"
echo "   -- ad valorem with a claim, and with legal-aid exemption:"
curl -s -X POST "$BASE/api/filing/fees/calculate" $CLERK $JSON \
  -d '{"case_type":"O.S","claim_value":750000}' \
  | $PY -c "import json,sys;d=json.load(sys.stdin);print(f\"   O.S claim 750000 -> {d['total']}\")"
curl -s -X POST "$BASE/api/filing/fees/calculate" $CLERK $JSON \
  -d '{"case_type":"O.S","claim_value":750000,"exemptions":["legal_aid"]}' \
  | $PY -c "import json,sys;d=json.load(sys.stdin);print(f\"   same, legal_aid  -> {d['total']} (waived {d['waived']})\")"

hr "4  Submit a DEFECTIVE filing — scrutiny must reject it"
NEXT_BEFORE=$(curl -s "$BASE/api/filing/case-types" $CLERK | $PY -c "
import json,sys
print([t['next_number'] for t in json.load(sys.stdin)['case_types'] if t['case_type']=='CRL.A'][0])")
echo "   next CRL.A number before: $NEXT_BEFORE"
curl -s -X POST "$BASE/api/filing" $CLERK $JSON -d "{
  \"case_type\":\"CRL.A\",
  \"title\":\"Ramesh Kumar v. State of Karnataka\",
  \"jurisdiction\":\"KA-BLR\",
  \"cause_of_action_date\":\"2026-01-05\",
  \"filing_date\":\"2026-11-13\",
  \"parties\":[{\"side\":\"petitioner\",\"name\":\"Ramesh Kumar\",\"advocate\":\"Sri. Vinay Hegde\"}],
  \"documents\":[{\"document_id\":\"$DOC_memorandum_of_appeal\",\"role\":\"memorandum_of_appeal\"}]
}" | $PY -c "
import json,sys; d=json.load(sys.stdin)
print(f\"   status={d['status']}  case_number={d['case_number']}\")
for o in d['objections']: print(f\"     ✗ {o['code']}: {o['message']}\")
print(f\"   {d['note']}\")"
NEXT_AFTER=$(curl -s "$BASE/api/filing/case-types" $CLERK | $PY -c "
import json,sys
print([t['next_number'] for t in json.load(sys.stdin)['case_types'] if t['case_type']=='CRL.A'][0])")
echo "   next CRL.A number after:  $NEXT_AFTER"
[ "$NEXT_BEFORE" = "$NEXT_AFTER" ] && ok "rejected filing consumed NO case number (no gap in the register)"

hr "5  Resubmit CLEAN — accepted, case number allocated"
curl -s -X POST "$BASE/api/filing" $CLERK $JSON -d "{
  \"case_type\":\"CRL.A\",
  \"title\":\"Ramesh Kumar v. State of Karnataka\",
  \"jurisdiction\":\"KA-BLR\",
  \"cause_of_action_date\":\"2026-10-01\",
  \"filing_date\":\"2026-11-13\",
  \"parties\":[
    {\"side\":\"petitioner\",\"name\":\"Ramesh Kumar\",\"advocate\":\"Sri. Vinay Hegde\",\"email\":\"ramesh@example.in\"},
    {\"side\":\"respondent\",\"name\":\"State of Karnataka\",\"advocate\":\"Smt. Deepa Shetty\",\"phone\":\"+919900000001\"}
  ],
  \"documents\":[
    {\"document_id\":\"$DOC_memorandum_of_appeal\",\"role\":\"memorandum_of_appeal\"},
    {\"document_id\":\"$DOC_certified_copy_of_judgment\",\"role\":\"certified_copy_of_judgment\"},
    {\"document_id\":\"$DOC_vakalatnama\",\"role\":\"vakalatnama\"}
  ]
}" > /tmp/e2e_filing.json
$PY -c "
import json; d=json.load(open('/tmp/e2e_filing.json'))
print(f\"   status={d['status']}  CASE NUMBER = {d['case_number']}\")
print(f\"   fee = {d['fee']['total']} {d['fee']['currency']}\")
for c in d['confirmations']: print(f\"     confirmation -> {c['party']:22} {c['channel'] or '-':6} {c['status']}\")
open('/tmp/e2e_case_id','w').write(d['case_id'])
open('/tmp/e2e_filing_id','w').write(d['filing_id'])"
CASE=$(cat /tmp/e2e_case_id)
ok "case created and linked to its filed documents"

hr "6  Scheduling — availability, then a real conflict"
# The slot is chosen from what is actually free, not hardcoded: re-running
# this script against a database that already holds hearings must still
# work, and a hardcoded hour silently becomes a conflict on the second run.
curl -s "$BASE/api/scheduling/availability?case_id=$CASE&judge_id=j-anand&courtroom_id=cr-1&day=2026-11-20&duration_minutes=30" $CLERK \
  > /tmp/e2e_avail.json
# Stage 7 re-books this same slot before the OTHER judge in the OTHER hall to
# isolate the advocate conflict, so the slot has to sit inside both judges'
# hours. Intersect the two availability lists rather than assume they overlap.
curl -s "$BASE/api/scheduling/availability?case_id=$CASE&judge_id=j-fatima&courtroom_id=cr-2&day=2026-11-20&duration_minutes=30" $CLERK \
  > /tmp/e2e_avail2.json
$PY - <<'PYEOF'
import json, sys
a = json.load(open('/tmp/e2e_avail.json'))
b = json.load(open('/tmp/e2e_avail2.json'))
print(f"   {a['free_count']}/{a['total_slots']} slots free on 2026-11-20")
free_a = [s for s in a['slots'] if s['free']]
free_b = {s['starts_at'] for s in b['slots'] if s['free']}
print('   first three free:', [s['starts_at'][11:16] for s in free_a[:3]])
both = [s for s in free_a if s['starts_at'] in free_b]
if not both:
    print('   No slot is free for BOTH judges today -- cannot continue stage 6.')
    print('   Wipe the database and re-seed, or pick another day.')
    sys.exit(1)
print(f"   usable by both judges: {both[0]['starts_at'][11:16]}")
open('/tmp/e2e_slot_start', 'w').write(both[0]['starts_at'])
open('/tmp/e2e_slot_end', 'w').write(both[0]['ends_at'])
PYEOF
[ -s /tmp/e2e_slot_start ] || exit 1
SLOT_START=$(cat /tmp/e2e_slot_start); SLOT_END=$(cat /tmp/e2e_slot_end)

echo "   schedule ${SLOT_START:11:5}-${SLOT_END:11:5}:"
curl -s -X POST "$BASE/api/scheduling/hearings" $CLERK $JSON -d "{
  \"case_id\":\"$CASE\",\"judge_id\":\"j-anand\",\"courtroom_id\":\"cr-1\",
  \"starts_at\":\"$SLOT_START\",\"ends_at\":\"$SLOT_END\",
  \"purpose\":\"Admission\"}" > /tmp/e2e_hearing.json
$PY - <<'PYEOF'
import json, sys
d = json.load(open('/tmp/e2e_hearing.json'))
if 'hearing_id' not in d:
    # Surface the server's actual answer rather than dying on a missing key.
    print('   SCHEDULING FAILED. Server said:')
    print('   ' + json.dumps(d, indent=2).replace(chr(10), chr(10) + '   '))
    sys.exit(1)
print(f"   hearing={d['hearing_id'][:8]}...  {d['starts_at']} -> {d['ends_at']}")
for r in d['reminders']:
    print(f"     reminder {r.get('channel','-'):6} {r.get('destination','-'):22} {r['status']}")
open('/tmp/e2e_hearing_id', 'w').write(d['hearing_id'])
PYEOF
[ -s /tmp/e2e_hearing_id ] || exit 1

echo "   now try the SAME slot again (judge + courtroom busy):"
curl -s -X POST "$BASE/api/scheduling/hearings" $CLERK $JSON -d "{
  \"case_id\":\"$CASE\",\"judge_id\":\"j-anand\",\"courtroom_id\":\"cr-1\",
  \"starts_at\":\"$SLOT_START\",\"ends_at\":\"$SLOT_END\"}" \
  | $PY -c "
import json,sys; d=json.load(sys.stdin)
for c in d['detail']['conflicts']: print(f\"     x {c['kind']}: {c['detail']}\")"

echo "   outside sitting hours (09:00) and on a court holiday (25 Dec):"
curl -s -X POST "$BASE/api/scheduling/hearings" $CLERK $JSON -d "{
  \"case_id\":\"$CASE\",\"judge_id\":\"j-anand\",\"courtroom_id\":\"cr-2\",
  \"starts_at\":\"2026-12-25T09:00:00\",\"ends_at\":\"2026-12-25T09:30:00\"}" \
  | $PY -c "
import json,sys; d=json.load(sys.stdin)
for c in d['detail']['conflicts']: print(f\"     x {c['kind']}: {c['detail']}\")"

hr "7  Advocate double-listing — the cross-court conflict"
# Second case, different judge AND different courtroom, but the SAME advocate.
curl -s -X POST "$BASE/api/filing" $CLERK $JSON -d "{
  \"case_type\":\"W.P\",\"title\":\"Another matter, same counsel\",
  \"jurisdiction\":\"KA-BLR\",
  \"parties\":[
    {\"side\":\"petitioner\",\"name\":\"Suresh N\",\"advocate\":\"Sri. Vinay Hegde\",\"email\":\"s@example.in\"},
    {\"side\":\"respondent\",\"name\":\"BBMP\",\"email\":\"bbmp@example.in\"}],
  \"documents\":[
    {\"document_id\":\"$DOC_memorandum_of_appeal\",\"role\":\"petition\"},
    {\"document_id\":\"$DOC_certified_copy_of_judgment\",\"role\":\"affidavit\"},
    {\"document_id\":\"$DOC_vakalatnama\",\"role\":\"vakalatnama\"}]
}" | $PY -c "
import json,sys; d=json.load(sys.stdin)
print(f\"   second case {d['case_number']} (advocate: Sri. Vinay Hegde)\")
open('/tmp/e2e_case2','w').write(d['case_id'] or '')"
CASE2=$(cat /tmp/e2e_case2)
echo "   schedule it at the same hour before a DIFFERENT judge in a DIFFERENT hall:"
curl -s -X POST "$BASE/api/scheduling/hearings" $CLERK $JSON -d "{
  \"case_id\":\"$CASE2\",\"judge_id\":\"j-fatima\",\"courtroom_id\":\"cr-2\",
  \"starts_at\":\"$SLOT_START\",\"ends_at\":\"$SLOT_END\"}" \
  | $PY -c "
import json,sys; d=json.load(sys.stdin)
if 'detail' in d:
    for c in d['detail']['conflicts']: print(f\"     ✗ {c['kind']}: {c['detail']}\")
else: print('     (scheduled — no conflict detected)')"
ok "judge and room were free; the advocate was not"

hr "8  Cause list"
curl -s "$BASE/api/scheduling/cause-list?day=2026-11-20" $JUDGE | $PY -c "
import json,sys; d=json.load(sys.stdin)
print(f\"   {d['court_id']}  {d['day']}  ({d['item_count']} items)\")
for i in d['items']:
    print(f\"     {i['item_no']}. {i['case_number']:18} {i['starts_at'][11:16]} \"
          f\"{i['judge']:34} {i['courtroom']}\")
    print(f\"        {i['title']}  advocates={i['advocates']}\")"

hr "9  Reminders queued (not sent)"
curl -s "$BASE/api/scheduling/reminders/due?before=2026-12-31T00:00:00" $CLERK | $PY -c "
import json,sys; d=json.load(sys.stdin)
print(f\"   {d['count']} reminder(s) a dispatch worker would pick up\")
for r in d['reminders']: print(f\"     {r['channel']:6} {r['destination']:22} for {r['case_number']} at {r['scheduled_for'][:16]}\")"

hr "10 Integration Hub — pay the court fee"
FEE=$($PY -c "import json;print(json.load(open('/tmp/e2e_filing.json'))['fee']['total'])")
KEY="fee-$(cat /tmp/e2e_filing_id)"
echo "   collect $FEE with Idempotency-Key=$KEY"
curl -s -X POST "$BASE/api/integrations/payments/invoke" $CLERK $JSON \
  -H "Idempotency-Key: $KEY" \
  -d "{\"operation\":\"collect\",\"payload\":{\"amount\":$FEE,\"currency\":\"INR\"}}" \
  | $PY -c "
import json,sys; d=json.load(sys.stdin); r=d['result']
print(f\"   replayed={d['replayed']} receipt={r['receipt_no']} mock={r['mock']}\")"
echo "   repeat the SAME key (must not charge twice):"
curl -s -X POST "$BASE/api/integrations/payments/invoke" $CLERK $JSON \
  -H "Idempotency-Key: $KEY" \
  -d "{\"operation\":\"collect\",\"payload\":{\"amount\":$FEE,\"currency\":\"INR\"}}" \
  | $PY -c "
import json,sys; d=json.load(sys.stdin); r=d['result']
print(f\"   replayed={d['replayed']} receipt={r['receipt_no']}  <- same receipt, no second charge\")"

hr "11 Integration Hub — other connectors, redaction, RBAC"
curl -s -X POST "$BASE/api/integrations/identity/invoke" $CLERK $JSON \
  -d '{"operation":"verify","payload":{"identity_number":"123456789012","name":"Ramesh Kumar"}}' \
  | $PY -c "
import json,sys; r=json.load(sys.stdin)['result']
print(f\"   identity: verified={r['verified']} hash={r['identity_hash']}\")
print(f\"             {r['note']}\")"
curl -s -X POST "$BASE/api/integrations/prison/invoke" $CLERK $JSON \
  -d '{"operation":"custody_status","payload":{"party_id":"p-123"}}' \
  | $PY -c "
import json,sys; r=json.load(sys.stdin)['result']
print(f\"   prison:   in_custody={r['in_custody']} video={r['video_link_available']}\")"
printf "   litigant -> identity connector: HTTP %s (expect 403)\n" \
  "$(code -X POST "$BASE/api/integrations/identity/invoke" \
     -H X-User-Id:lit-1 -H X-Role:litigant -H X-Jurisdiction:KA-BLR $JSON \
     -d '{"operation":"verify","payload":{"identity_number":"123456789012"}}')"

echo "   call log (identity number must be redacted):"
curl -s "$BASE/api/integrations/calls?limit=3" $REG | $PY -c "
import json,sys
for c in json.load(sys.stdin)['calls']:
    print(f\"     {c['connector_id']:12} {c['operation']:16} {c['status']:7} \"
          f\"attempts={c['attempts']}  req={c['request_json']}\")"

hr "12 Circuit breaker"
for i in 1 2 3; do
  code -X POST "$BASE/api/integrations/legal_aid/invoke" $CLERK $JSON \
    -d '{"operation":"check_eligibility","payload":{},"_fail_injection":true}' >/dev/null
done
printf "   after 3 induced failures -> HTTP %s (503 = breaker open, not calling upstream)\n" \
  "$(code -X POST "$BASE/api/integrations/legal_aid/invoke" $CLERK $JSON \
      -d '{"operation":"check_eligibility","payload":{"annual_income":120000}}')"
curl -s "$BASE/api/integrations" $CLERK | $PY -c "
import json,sys
for c in json.load(sys.stdin)['connectors']:
    print(f\"     {c['connector_id']:14} circuit={c['circuit']:7} fails={c['consecutive_fails']}\")"

hr "13 The case, assembled from all four features"
curl -s "$BASE/api/filing/cases/$CASE" $JUDGE | $PY -c "
import json,sys; d=json.load(sys.stdin)
print(f\"   {d['case_number']}  {d['title']}\")
print(f\"   status={d['status']} filed={d['filed_at'][:10]}\")
for p in d['parties']: print(f\"     {p['side']:11} {p['name']:22} adv={p['advocate']}\")
for h in d['hearings']: print(f\"     hearing {h['starts_at']} {h['judge_name']} ({h['status']})\")"

hr "DONE"
