# Court Case Management System

All four specified features are built. Two caveats are stated up front
rather than buried: the five Integration Hub connectors are **mocks** (the
framework around them is real), and the court fee schedule is a
**placeholder**, not the Karnataka Court Fees and Suits Valuation Act.
Nothing here pretends to work that doesn't.

| Feature | Status |
|---|---|
| Document Management | Implemented, backed by Landing AI Agentic Document Extraction |
| Case Initiation & Filing | Implemented; fee schedule is a placeholder |
| Scheduling & Hearings | Implemented |
| Integration Hub | Framework implemented; all five adapters are mocks |

A browser test console for all four is served at `/ui`.

## Run it

```bash
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp .env.example .env     # then set VISION_AGENT_API_KEY
./.venv/bin/python seed.py                   # judges, courtrooms, holidays
./.venv/bin/python -m uvicorn app.main:app --reload --port 8020
```

Then open **`http://localhost:8020/ui`**. Interactive API docs are at `/docs`,
and `GET /health` reports which features are implemented and whether document
extraction is configured.

## The test console at /ui

A single static page, no build step, served by the API itself. It exists to
make the system's behaviour visible, refusals included.

Because authentication is a header stub, the page carries a **principal
switcher** at the top: user id, role and jurisdiction go out with every
request. Changing the role changes what comes back, which is the quickest way
to see the access rules work. A **request log** docked at the bottom records
the method, path, status and duration of every call the page makes.

What each tab is for:

- **Documents** - upload a filing and watch Landing AI pull the fields out of
  it, search the extracted text, open a document to sign, seal, re-extract,
  verify integrity or read its access trail.
- **Case filing** - the fee calculator with its line breakdown and ad valorem
  slabs; a filing form whose *Validate* button runs scrutiny without
  submitting; the register of filed cases.
- **Scheduling** - an availability grid where blocked slots are struck through
  and name the check that refused them. Click a free slot to load it into the
  form. Conflicts come back as a table, not an error string.
- **Integration hub** - the connector registry with live circuit state, an
  invoke form with a sample payload per operation, an *induce failure* box for
  opening the breaker, and the redacted call log.

Four things are worth trying deliberately, because they are the behaviours
most easily faked:

1. Set the role to `public` and search. The restricted document disappears
   from the results rather than returning a 403.
2. Submit a filing with nothing attached. Every objection comes back at once,
   and the next case number on the Case types card does not move.
3. Book a slot, then book the same slot again for a different case before a
   different judge in a different hall. It is still refused, because the
   advocate is already listed elsewhere at that hour.
4. Invoke a payment, then press *Repeat with same key*. The second call
   replays the first receipt instead of charging again.

## Testing it

Three suites, all against a running server:

```bash
./test_end_to_end.sh                        # all four features, filing through hearing
./test_document_management.sh               # documents in depth (spends ADE credits)
./test_document_management.sh --no-upload   # ...reusing the last upload instead
./.venv/bin/python test_ui.py               # drives the browser UI (39 checks)
./.venv/bin/python test_ui.py --headed      # ...visibly
```

`test_ui.py` needs Playwright:

```bash
./.venv/bin/pip install playwright && ./.venv/bin/python -m playwright install chromium
```

The shell suites are safe to re-run against a database that already holds
data: the scheduling stage picks a slot that is actually free rather than
assuming one. Case numbers climb with each run, which is the register working
correctly, not a leak. To start clean, stop the server and

```bash
rm -rf data/court.db* data/blobs && ./.venv/bin/python seed.py
```

## Document Management

Secure, searchable document store with version control, digital signatures,
court seal stamping, and access permissions by role and jurisdiction.

### Authentication is a deliberate stub

Every request carries three headers standing in for real auth:

```
X-User-Id: clerk-01
X-Role: judge | registrar | clerk | advocate | litigant | prison_officer | public
X-Jurisdiction: KA-BLR
```

`app/security.py:current_principal` is the one function to replace with
JWT/SSO verification. Nothing else changes when you do, because every
policy decision works off the resulting `Principal`, not off headers.

### Access control

Documents carry a `classification` (`public` / `restricted` / `sealed`) and
a `jurisdiction`. Reads need both a role cleared for the classification and
a jurisdiction match — `registrar` is the only role with cross-jurisdiction
reach, and `public` classification is not jurisdiction-scoped.

Refused reads return **404, not 403**: confirming that a sealed matter
exists in another jurisdiction is itself a disclosure.

| | upload | sign | seal | audit trail |
|---|---|---|---|---|
| judge | ✓ | ✓ | ✓ | ✓ |
| registrar | ✓ | ✓ | ✓ | ✓ |
| clerk | ✓ | | | |
| advocate | ✓ | ✓ | | |
| litigant | ✓ | | | |
| prison_officer, public | | | | |

Every read and every refusal lands in an append-only `access_log`.

### Landing AI extraction

Upload runs ADE's two-step pipeline:

1. `parse()` → the document as **Markdown** plus a structure tree of pages
   and positioned elements. The Markdown is what gets indexed for search —
   a scanned PDF is unsearchable until this runs.
2. `extract()` → typed fields against the `CourtDocumentFields` schema in
   `app/services/extraction.py`: case number, court, parties, advocates,
   statutes cited, relief sought, filing and hearing dates.

Why ADE rather than Tesseract: court filings are tables, stamps, annexures
and multi-column cause lists. Plain OCR flattens that into a word stream
with the structure destroyed; ADE preserves tables and reading order, and
each element carries its page and bounding box.

**Degradation is deliberate.** With no API key, or when the API fails, the
upload still stores and versions the file — the version row records
`extraction_status` of `skipped`/`failed` plus the reason, and
`POST /api/documents/{id}/versions/{n}/reextract` retries later against the
stored bytes. Losing a filing because a third-party API was down is not an
acceptable failure mode.

### Versioning and integrity

Blobs are stored **content-addressed by SHA-256**, not by filename. Three
properties follow: tamper evidence (the path *is* the hash), deduplication
(a re-filed annexure costs one copy), and no path traversal via upload
name. Versions are immutable — a correction is a new row, never an
`UPDATE`. Re-uploading identical bytes is rejected with `409`.

### Signatures and seals

Both bind an HMAC-SHA256 to the **exact content hash** being attested, so
an attestation covers the bytes that existed when it was made. Three
independent conditions are reported separately, because they fail
differently:

- `signature_valid` — the HMAC matches
- `covers_current_content` — the signed hash is still that version's hash
- `blob_verified` — the bytes on disk *still hash to* the signed hash
  (`null` on plain reads, which don't re-hash; `true`/`false` only from
  `/integrity`, which does)

`valid` requires all three, and never reports `true` when `blob_verified`
is `false`.

**Scope limit, stated plainly:** HMAC proves *this server* attested. It
does not prove *which judge* attested to a third party, because verifying
needs the same secret that signing needs. That is adequate for in-system
tamper evidence and **inadequate as a legal e-signature**. Production needs
asymmetric keys — X.509/PKCS#7 detached signatures, per-officer keys in an
HSM or smart card — so a third party can verify without being able to
forge. `app/services/seals.py:sign_payload` is the single seam for that
swap.

## Case Initiation & Filing

Electronic filing with fee calculation, document scrutiny, case-number
allocation and confirmation to all parties.

The order of operations is the design decision worth pointing at:
**validate, then compute the fee, then allocate a number.** A filing that
fails scrutiny never reaches the allocator, so it never burns a case
number. Gaps in a court register are not cosmetic; a missing serial number
invites exactly the question of what was removed from the record.

Numbers come from `app/services/casenumbers.py`, allocated with

```sql
INSERT ... ON CONFLICT DO UPDATE SET last_no = last_no + 1 RETURNING last_no
```

inside the caller's transaction, so a rolled-back filing rolls back its
number too. The series is per court, per case type, per year: `CRL.A
2/2026`.

Scrutiny (`app/services/validation.py`) returns **every** objection at
once rather than the first one. Four defects should mean one resubmission,
not four. It checks required documents per case type, that a respondent
exists, that every party has an address for service, that the filer's role
may file at all, and the limitation period against the cause-of-action
date. A rejected filing is still recorded with its objections, because the
attempt and its date are part of the record.

### The fee schedule is a placeholder

`app/services/fees.py` carries two dated schedules (2020 and 2024) and
picks by filing date, so a filing dated before a revision is charged at
the old rate. It supports flat fees, ad valorem on claim value, statutory
surcharges, and the `legal_aid`, `in_forma_pauperis` and
`state_government` exemptions.

The **numbers are invented.** Real court fees come from a state statute and
its schedules, are revised by notification, and differ per court. Replacing
`SCHEDULES` with the gazetted figures is the only change needed; the
versioning machinery is what matters structurally.

## Scheduling & Hearings

Automated scheduling with conflict detection, cause lists and reminders.

`app/services/calendar.py` checks six kinds of conflict before a hearing is
accepted, and a clash returns `409` with all of them listed:

- judge already listed in that window
- courtroom occupied
- **advocate double-listed, including across courts** — this one is
  deliberately not scoped to the court being scheduled, because the whole
  point of the check is to catch an advocate listed simultaneously in two
  different courts
- court holiday
- outside that judge's sitting hours
- a video-link hearing assigned to a hall without video facilities

Adjournment creates the next hearing and leaves the original in the record
with its reason, rather than mutating a row. The cause list orders
part-heard matters first, then scheduled, then the rest.

Reminders are **queued, not sent.** `GET /api/scheduling/reminders/due`
returns what a dispatch worker would pick up. There is no SMTP or SMS
credential in this scaffold, and a system that claims to have notified a
litigant when it did not is worse than one that admits it has no transport.

## Integration Hub

Connectors for prison systems, payment gateways, national identity
registers, legal aid authorities and bar association directories.

**Every adapter is a mock.** There are no upstream systems or credentials
here, and each response carries `"mock": true` so synthetic data cannot be
mistaken for an upstream answer. What is real is the framework in
`app/services/connectors.py`:

- **Idempotency.** Pass an `Idempotency-Key` header and a key that already
  succeeded returns the stored response instead of calling again. A
  double-charged court fee or a duplicate produce warrant are each serious
  enough that this is the default expectation, not an optimisation.
- **Retry** with exponential backoff, three attempts.
- **Circuit breaker.** Three consecutive failures opens the circuit for two
  minutes; calls then fail fast with `503` plus `Retry-After` instead of
  queueing behind a dead upstream.
- **Redaction before logging.** Identity numbers, card numbers, CVVs,
  passwords and OTPs never reach the call log. The identity connector
  hashes the number it receives and stores only the hash.
- **Per-connector role gates.** A clerk settling a fee is ordinary; a clerk
  querying the national identity register is not. The outbound call log is
  readable by judges and registrars only, and is kept separate from the
  document access log.

## Endpoints

```
Document Management
POST   /api/documents                                 upload + extract (v1)
GET    /api/documents                                 list (access-filtered)
GET    /api/documents/search?q=                       full-text over extracted text
GET    /api/documents/{id}                            detail + all versions + attestations
POST   /api/documents/{id}/versions                   supersede with a new version
GET    /api/documents/{id}/content?version=           original file bytes
GET    /api/documents/{id}/text?version=              extracted Markdown + fields
POST   /api/documents/{id}/versions/{n}/reextract     retry Landing AI
POST   /api/documents/{id}/versions/{n}/sign          digital signature
POST   /api/documents/{id}/versions/{n}/seal          court seal
GET    /api/documents/{id}/integrity                  re-hash + re-verify everything
GET    /api/documents/{id}/audit                      access trail (judge/registrar)

Case Initiation & Filing
GET    /api/filing/case-types                         case types, requirements, next number
POST   /api/filing/fees/calculate                     fee with line breakdown
POST   /api/filing/validate                           pre-flight scrutiny, no submission
POST   /api/filing                                    submit a filing
GET    /api/filing/{filing_id}                        filing, objections, fee, documents
GET    /api/filing/cases/{case_id}                    case with parties and hearings
GET    /api/filing                                    list cases (access-filtered)

Scheduling & Hearings
GET    /api/scheduling/resources                      judges, courtrooms, holidays
POST   /api/scheduling/resources/{judges|courtrooms|holidays}   register resources
GET    /api/scheduling/availability                   free slots for a judge on a date
POST   /api/scheduling/hearings                       schedule (409 + conflicts on clash)
GET    /api/scheduling/hearings/{id}                  hearing detail
POST   /api/scheduling/hearings/{id}/adjourn          adjourn to a new slot
GET    /api/scheduling/cause-list?court_id=&on=       daily cause list
POST   /api/scheduling/reminders                      queue reminders for a hearing
GET    /api/scheduling/reminders/due                  what a dispatch worker would send

Integration Hub
GET    /api/integrations                              registry + live circuit state
POST   /api/integrations/{connector}/invoke           call one operation
GET    /api/integrations/calls                        outbound call log (judge/registrar)

Meta
GET    /ui                                            browser test console
GET    /docs                                          OpenAPI explorer
GET    /health                                        feature + extraction status
```

## Verified end to end

`./test_end_to_end.sh` runs the whole pipeline against a live server and
the live Landing AI API. Last run, in order:

- three documents uploaded, parsed and indexed; extraction returned
  `page_count=1`, `element_count=8` and correctly pulled case number
  `CRL.P. No. 4471/2025`, the court, both advocates, petitioner and
  respondents, all three statutes cited, and both dates
- fee versioning: the same filing cost `975.00` under the 2024 schedule and
  `790.00` dated before it; an ad valorem suit on a claim of `750,000` came
  to `14,375.00`, waived to `0.00` with `legal_aid`
- a defective filing was rejected with all five objections at once
  (two missing documents, no respondent, a party with no address for
  service, and `time_barred` at 312 days against a 90-day limitation), and
  **consumed no case number** — the next number was unchanged afterwards
- the corrected filing was accepted as `CRL.A 2/2026`, with confirmations
  queued to both parties
- a hearing was scheduled at 11:00; the same slot requested again returned
  `judge_busy` and `courtroom_busy`; 09:00 returned
  `outside_sitting_hours` and 25 December returned `court_holiday`
- a second case before a **different judge in a different hall** at the
  same hour was still refused: `advocate_double_listed`
- the cause list rendered, and two reminders sat queued for dispatch
- the court fee was collected through the payments connector; repeating the
  call with the same `Idempotency-Key` replayed the original receipt
  instead of charging twice
- the identity connector returned a hash, and the call log showed
  `"identity_number": "***redacted***"`
- a litigant calling the identity connector got `403`
- three induced failures opened the legal-aid circuit, after which calls
  returned `503` without touching the adapter

The browser console is covered by `test_ui.py`, which drives a real Chromium
against `/ui` and asserts on what reaches the DOM: 39 checks over all four
tabs, including that a `public` principal sees fewer documents than a clerk,
that re-booking a slot surfaces the conflict table, that repeating an
idempotency key reports a replay, and that no JavaScript error reaches the
console. A page that renders but reads the wrong JSON key looks fine in a
screenshot and is useless in practice.

Document Management is covered in more depth by
`./test_document_management.sh`, including version supersession, the
access-control matrix (judge `200`, `public` `404`, other-jurisdiction
clerk `404`, registrar cross-jurisdiction `200`), search hits and misses,
role gates on signing and sealing, and the tamper test: appending one byte
to a stored blob flips `intact` to `false` and every attestation to
`valid=false` while `signature_valid` stays `true`, distinguishing
"signature is fine, the file changed underneath it".

## Known gaps

- **Auth is headers** (see above) — the intended seam, not an oversight.
- **The five connectors are mocks.** Replacing one means rewriting a single
  function in `app/services/connectors.py`; the idempotency, retry,
  breaker, redaction and logging around it stay.
- **The fee schedule is invented** (see above). Replace `SCHEDULES` with
  gazetted figures.
- **Reminders are queued, never sent.** No mail or SMS transport is wired.
- **Extraction is synchronous.** A 300-page scan blocks the upload request
  for as long as ADE takes. Production wants a job queue, with the version
  row created immediately at `extraction_status='pending'` and filled in by
  a worker; `reextract` already provides that shape.
- **HMAC, not PKI** for signatures (see above).
- **SQLite.** Fine for a scaffold and real FTS5 search with no extra
  infrastructure; one writer at a time means it is not the production
  answer. Every query is in `app/db.py`, so swapping to Postgres means
  replacing that module, not the routers.
- **The UI is a test console, not a court clerk's workstation.** It exposes
  the API surface for inspection, with a role switcher where a real deployment
  would have a login. It has no pagination, no drafts, no printing, and it
  assumes one court.
