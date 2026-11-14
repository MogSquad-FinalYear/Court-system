"use strict";
// Test console for the court API. Every call goes through api(), which logs
// method, path and status to the drawer at the bottom -- the point of this
// page is to make the API's behaviour visible, including its refusals.

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");
const json = (o) => `<pre class="json">${esc(JSON.stringify(o, null, 2))}</pre>`;
const msg = (kind, text) => `<div class="msg ${kind}">${text}</div>`;
const today = (offsetDays = 0) => {
  const d = new Date(); d.setDate(d.getDate() + offsetDays);
  return d.toISOString().slice(0, 10);
};

function headers(extra) {
  return Object.assign({
    "X-User-Id": $("p-user").value.trim() || "anon",
    "X-Role": $("p-role").value,
    "X-Jurisdiction": $("p-jur").value,
  }, extra || {});
}

// ---------------------------------------------------------------- request log
let calls = 0;
function log(method, path, status, ms) {
  calls += 1;
  $("log-count").textContent = `${calls} call${calls === 1 ? "" : "s"}`;
  const cls = status >= 500 ? "s5" : status >= 400 ? "s4" : "s2";
  const row = document.createElement("div");
  row.className = "r";
  row.innerHTML = `<span class="s ${cls}">${status}</span>` +
    `<span class="m">${esc(method)}</span>` +
    `<span class="p">${esc(path)}</span><span class="t">${ms} ms</span>`;
  const body = $("log-body");
  body.prepend(row);
  while (body.children.length > 200) body.lastChild.remove();
}

async function api(method, path, opts) {
  opts = opts || {};
  const t0 = performance.now();
  const init = { method, headers: headers(opts.headers) };
  if (opts.form) {
    init.body = opts.form;                       // browser sets the boundary
  } else if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  let res, data;
  try {
    res = await fetch(path, init);
  } catch (err) {
    log(method, path, 0, Math.round(performance.now() - t0));
    return { ok: false, status: 0, data: { detail: `Network error: ${err.message}. Is the server running?` } };
  }
  const ms = Math.round(performance.now() - t0);
  const ct = res.headers.get("content-type") || "";
  data = ct.includes("application/json") ? await res.json().catch(() => null)
                                         : await res.text();
  log(method, path, res.status, ms);
  return { ok: res.ok, status: res.status, data };
}

// Renders the server's own words on failure rather than a generic message.
function problem(r) {
  const d = r.data && r.data.detail !== undefined ? r.data.detail : r.data;
  if (d && d.conflicts) {
    return msg("bad", `<strong>${esc(d.message || "Refused")}</strong> (HTTP ${r.status})`) +
      "<table><tr><th>conflict</th><th>detail</th></tr>" +
      d.conflicts.map((c) => `<tr><td><span class="tag bad">${esc(c.kind)}</span></td>` +
        `<td>${esc(c.detail)}</td></tr>`).join("") + "</table>";
  }
  if (Array.isArray(d)) {   // FastAPI validation errors
    return msg("bad", `HTTP ${r.status}`) + json(d);
  }
  return msg("bad", `<strong>HTTP ${r.status}</strong> &mdash; ${esc(typeof d === "string" ? d : JSON.stringify(d))}`);
}

function busy(btn, fn) {
  return async (...a) => {
    btn.disabled = true;
    try { await fn(...a); } finally { btn.disabled = false; }
  };
}

// ---------------------------------------------------------------------- tabs
document.querySelectorAll("nav button").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("nav button").forEach((x) => x.classList.toggle("on", x === b));
    ["documents", "filing", "scheduling", "integrations"].forEach((t) => {
      $("tab-" + t).hidden = (t !== b.dataset.tab);
    });
  };
});
$("log-head").onclick = () => {
  const open = $("logbar").classList.toggle("open");
  $("log-hint").textContent = open ? "click to collapse" : "click to expand";
};

// -------------------------------------------------------------------- health
async function health() {
  const r = await api("GET", "/health");
  if (!r.ok) { $("h-health").textContent = "server unreachable"; return; }
  const f = r.data.features || {};
  const n = Object.values(f).filter((v) => String(v).startsWith("implemented")).length;
  $("h-health").textContent = `${n}/4 features implemented`;
  const ade = r.data.document_extraction_configured;
  $("h-ade").textContent = ade ? "Landing AI: configured" : "Landing AI: NOT configured";
  $("h-ade").style.background = ade ? "rgba(123,212,160,.22)" : "rgba(240,138,126,.25)";
}

// ===================================================================
// DOCUMENTS
// ===================================================================
let selectedDoc = null;

$("u-go").onclick = busy($("u-go"), async () => {
  const file = $("u-file").files[0];
  if (!file) { $("u-out").innerHTML = msg("warn", "Choose a file first."); return; }
  const fd = new FormData();
  fd.append("file", file);
  fd.append("title", $("u-title").value || file.name);
  fd.append("jurisdiction", $("u-jur").value);
  fd.append("case_number", $("u-case").value);
  fd.append("doc_type", $("u-type").value);
  fd.append("classification", $("u-class").value);
  $("u-out").innerHTML = msg("warn", "Uploading, then parsing with Landing AI. This takes a few seconds.");
  const r = await api("POST", "/api/documents", { form: fd });
  if (!r.ok) { $("u-out").innerHTML = problem(r); return; }
  const v = r.data.versions[0];
  const st = v.extraction_status;
  $("u-out").innerHTML =
    msg(st === "extracted" ? "ok" : "warn",
        `Stored as <code>${esc(r.data.document_id)}</code> &mdash; extraction <strong>${esc(st)}</strong>` +
        (st === "extracted" ? `, ${v.page_count || "?"} page(s), ${v.element_count || "?"} elements`
                            : `. The file is stored and versioned anyway; retry with reextract.`)) +
    (v.extraction_error ? msg("bad", esc(v.extraction_error)) : "") +
    fieldsTable(v.fields, v.version_no);
  selectDoc(r.data.document_id);
  loadDocPicker();
});

function fieldsTable(fields, versionNo) {
  if (!fields) return "";
  // Only fields the extractor actually found: a column of blanks says nothing.
  const found = Object.entries(fields).filter(
    ([, v]) => v != null && v !== "" && !(Array.isArray(v) && !v.length));
  const label = versionNo ? `extracted from v${versionNo}` : "extracted";
  if (!found.length) {
    return msg("warn", `No fields ${esc(label)}. The document may not carry them, or extraction was skipped.`);
  }
  return `<table><tr><th>field</th><th>value Landing AI ${esc(label)}</th></tr>` +
    found.map(([k, v]) =>
      `<tr><td class="mono">${esc(k)}</td><td>${esc(Array.isArray(v) ? v.join("; ") : v)}</td></tr>`
    ).join("") + "</table>";
}

function docRows(list, label) {
  if (!list.length) return `<div class="empty">${label}</div>`;
  return `<div class="scroll"><table><tr><th>title</th><th>case</th><th>type</th><th>class</th><th>v</th></tr>` +
    list.map((d) => `<tr class="click" data-doc="${esc(d.document_id || d.id)}">` +
      `<td>${esc(d.title)}${d.excerpt ? `<div class="mono" style="color:var(--muted)">${esc(d.excerpt.slice(0, 110))}</div>` : ""}</td>` +
      `<td class="mono">${esc(d.case_number || "-")}</td>` +
      `<td>${esc(d.doc_type || "-")}</td>` +
      `<td><span class="tag neutral">${esc(d.classification || "-")}</span></td>` +
      `<td>${esc(d.version_no || d.version_count || "")}</td></tr>`).join("") +
    "</table></div>";
}

function wireDocRows(container) {
  container.querySelectorAll("tr[data-doc]").forEach((tr) => {
    tr.onclick = () => selectDoc(tr.dataset.doc);
  });
}

$("s-go").onclick = busy($("s-go"), async () => {
  const q = $("s-q").value.trim();
  if (!q) return;
  const r = await api("GET", `/api/documents/search?q=${encodeURIComponent(q)}`);
  if (!r.ok) { $("s-out").innerHTML = problem(r); return; }
  $("s-out").innerHTML = msg(r.data.length ? "ok" : "warn",
    `${r.data.length} hit(s) visible to <strong>${esc($("p-role").value)}</strong> in ${esc($("p-jur").value)}`) +
    docRows(r.data, "No matches, or none you are cleared to see.");
  wireDocRows($("s-out"));
});

$("s-all").onclick = busy($("s-all"), async () => {
  const r = await api("GET", "/api/documents?limit=50");
  if (!r.ok) { $("s-out").innerHTML = problem(r); return; }
  $("s-out").innerHTML = msg("ok", `${r.data.length} document(s) visible to you`) +
    docRows(r.data, "Nothing visible. Upload something, or switch role.");
  wireDocRows($("s-out"));
});

async function selectDoc(id) {
  selectedDoc = id;
  $("d-out").innerHTML = `<div class="empty">Loading ${esc(id)}…</div>`;
  const r = await api("GET", `/api/documents/${id}`);
  if (!r.ok) {
    $("d-out").innerHTML = problem(r) + (r.status === 404
      ? msg("warn", "404 is deliberate for an unauthorised read: confirming a sealed matter exists in another jurisdiction is itself a disclosure.")
      : "");
    return;
  }
  const d = r.data;
  const latest = d.versions[d.versions.length - 1];
  $("d-out").innerHTML = `
    <dl class="kv">
      <dt>id</dt><dd class="mono">${esc(d.document_id)}</dd>
      <dt>title</dt><dd>${esc(d.title)}</dd>
      <dt>case number</dt><dd class="mono">${esc(d.case_number || "-")}</dd>
      <dt>classification</dt><dd><span class="tag neutral">${esc(d.classification)}</span> in ${esc(d.jurisdiction)}</dd>
    </dl>
    <div class="btns" style="margin:12px 0">
      <button class="b" id="d-text">Extracted text</button>
      <button class="b" id="d-integrity">Verify integrity</button>
      <button class="b" id="d-audit">Access log</button>
      <button class="b" id="d-dl">Download v${esc(latest.version_no)}</button>
    </div>
    <div class="scroll"><table>
      <tr><th>v</th><th>uploaded</th><th>by</th><th>extraction</th><th>hash</th><th>attestations</th><th></th></tr>
      ${d.versions.map((v) => `<tr>
        <td><strong>${v.version_no}</strong></td>
        <td>${esc((v.uploaded_at || "").slice(0, 19).replace("T", " "))}</td>
        <td class="mono">${esc(v.uploaded_by || "-")}</td>
        <td><span class="tag ${v.extraction_status === "extracted" ? "ok" : "warn"}">${esc(v.extraction_status)}</span></td>
        <td class="mono">${esc((v.content_hash || "").slice(0, 10))}…</td>
        <td>${(v.attestations || []).length
            ? (v.attestations || []).map((a) =>
                `<span class="tag ${a.valid ? "ok" : "bad"}">${esc(a.kind)}${a.valid ? "" : " invalid"}</span>`).join(" ")
            : '<span class="tag neutral">none</span>'}</td>
        <td>
          <button class="b sign" data-v="${v.version_no}">Sign</button>
          <button class="b seal" data-v="${v.version_no}">Seal</button>
          <button class="b rex" data-v="${v.version_no}">Re-extract</button>
        </td></tr>`).join("")}
    </table></div>
    ${fieldsTable(latest.fields, latest.version_no)}
    <div class="out" id="d-sub"></div>`;

  $("d-text").onclick = async () => {
    const t = await api("GET", `/api/documents/${id}/text`);
    $("d-sub").innerHTML = t.ok
      ? msg("ok", `extraction_status = <strong>${esc(t.data.extraction_status)}</strong> (latest version)`) +
        `<pre class="json">${esc(t.data.markdown || "(no text)")}</pre>`
      : problem(t);
  };
  $("d-integrity").onclick = async () => {
    const t = await api("GET", `/api/documents/${id}/integrity`);
    if (!t.ok) { $("d-sub").innerHTML = problem(t); return; }
    $("d-sub").innerHTML = msg(t.data.intact ? "ok" : "bad",
      t.data.intact ? "Every stored blob still hashes to its recorded hash, and every attestation still covers it."
                    : "Bytes on disk no longer match the recorded hash. Attestations over the changed version are now invalid.") +
      json(t.data);
  };
  $("d-audit").onclick = async () => {
    const t = await api("GET", `/api/documents/${id}/audit`);
    if (!t.ok) {
      $("d-sub").innerHTML = problem(t) + msg("warn", "Only a judge or registrar may read the access trail. Switch role and try again.");
      return;
    }
    const entries = t.data.entries || [];
    $("d-sub").innerHTML = msg("ok", `${entries.length} entry/entries, newest first. Refusals are logged too.`) +
      `<div class="scroll"><table><tr><th>when</th><th>who</th><th>role</th><th>action</th><th>allowed</th><th>detail</th></tr>` +
      entries.map((e) => `<tr><td>${esc((e.at || "").slice(0, 19).replace("T", " "))}</td>
        <td class="mono">${esc(e.actor_id)}</td><td>${esc(e.actor_role)}</td><td>${esc(e.action)}</td>
        <td><span class="tag ${e.allowed ? "ok" : "bad"}">${e.allowed ? "ALLOWED" : "REFUSED"}</span></td>
        <td>${esc(e.detail || "")}</td></tr>`).join("") + "</table></div>";
  };
  $("d-dl").onclick = () => {
    // Content needs the role headers, so fetch it and hand over a blob URL.
    api("GET", `/api/documents/${id}/content`).then(() => {
      fetch(`/api/documents/${id}/content`, { headers: headers() })
        .then((res) => res.blob())
        .then((b) => window.open(URL.createObjectURL(b), "_blank"));
    });
  };
  $("d-out").querySelectorAll("button.sign, button.seal, button.rex").forEach((b) => {
    b.onclick = busy(b, async () => {
      const kind = b.classList.contains("sign") ? "sign" : b.classList.contains("seal") ? "seal" : "reextract";
      const fd = new FormData();
      if (kind !== "reextract") fd.append("reason", kind === "sign" ? "Filed on behalf of party" : "Admitted for hearing");
      const t = await api("POST", `/api/documents/${id}/versions/${b.dataset.v}/${kind}`,
                          kind === "reextract" ? {} : { form: fd });
      if (!t.ok) {
        $("d-sub").innerHTML = problem(t) + msg("warn",
          "Signing needs judge, registrar or advocate; sealing needs judge or registrar. Every refusal is written to the access log too.");
        return;
      }
      await selectDoc(id);
      $("d-sub").innerHTML = msg("ok", `${kind} applied`) + json(t.data);
    });
  });
}

// ===================================================================
// FILING
// ===================================================================
let caseTypes = [], exemptions = {};

async function loadCaseTypes() {
  const r = await api("GET", "/api/filing/case-types");
  if (!r.ok) { $("ct-out").innerHTML = problem(r); return; }
  caseTypes = r.data.case_types; exemptions = r.data.exemptions || {};
  $("ct-out").innerHTML = `<div class="scroll"><table>
    <tr><th>type</th><th>required documents</th><th>limitation</th><th>next number</th></tr>` +
    caseTypes.map((c) => `<tr><td class="mono"><strong>${esc(c.case_type)}</strong></td>
      <td>${c.required_documents.map((d) => `<span class="tag neutral">${esc(d)}</span>`).join(" ")}</td>
      <td>${c.limitation_days ? esc(c.limitation_days) + " days" : '<span style="color:var(--faint)">none</span>'}</td>
      <td class="mono">${esc(c.next_number)}</td></tr>`).join("") + "</table></div>";

  for (const sel of [$("fe-type"), $("nf-type")]) {
    sel.innerHTML = caseTypes.map((c) => `<option>${esc(c.case_type)}</option>`).join("");
  }
  const boxes = Object.entries(exemptions).map(([k, v]) =>
    `<label class="inline" style="flex:0 0 auto"><input type="checkbox" value="${esc(k)}"> ${esc(v)}</label>`).join("");
  $("fe-ex").innerHTML = boxes;
  $("nf-ex").innerHTML = boxes;
}

const checked = (el) => Array.from(el.querySelectorAll("input:checked")).map((i) => i.value);

$("fe-go").onclick = busy($("fe-go"), async () => {
  const r = await api("POST", "/api/filing/fees/calculate", {
    body: {
      case_type: $("fe-type").value,
      claim_value: $("fe-claim").value ? Number($("fe-claim").value) : null,
      filing_date: $("fe-date").value || null,
      exemptions: checked($("fe-ex")),
    },
  });
  if (!r.ok) { $("fe-out").innerHTML = problem(r); return; }
  const d = r.data;
  $("fe-out").innerHTML = msg("ok",
    `<strong>${d.total.toFixed(2)} ${esc(d.currency)}</strong> under the schedule effective ${esc(d.schedule_effective_from)}` +
    (d.waived ? ` &mdash; ${d.waived.toFixed(2)} waived from a subtotal of ${d.subtotal.toFixed(2)}` : "")) +
    "<table><tr><th>line</th><th style='text-align:right'>amount</th></tr>" +
    (d.lines || []).map((l) => `<tr><td>${esc(l.label)}</td>
      <td style="text-align:right" class="mono">${Number(l.amount).toFixed(2)}</td></tr>`).join("") +
    "</table>" +
    (d.ad_valorem_slabs && d.ad_valorem_slabs.length
      ? "<table style='margin-top:10px'><tr><th>slab</th><th>rate</th><th style='text-align:right'>amount</th></tr>" +
        d.ad_valorem_slabs.map((x) => `<tr><td class="mono">${esc(x.slab)}</td><td>${(x.rate * 100).toFixed(2)}%</td>
          <td style="text-align:right" class="mono">${Number(x.amount).toFixed(2)}</td></tr>`).join("") + "</table>"
      : "") +
    msg("warn", esc(d.disclaimer || "Placeholder figures, not the state statute."));
});

function partyRow(p) {
  p = p || {};
  const div = document.createElement("div");
  div.className = "party";
  div.innerHTML = `<button class="x" title="remove">&times;</button>
    <div class="row">
      <label class="f"><span>Side</span><select class="p-side">
        <option>petitioner</option><option>respondent</option></select></label>
      <label class="f"><span>Name</span><input class="p-name"></label>
      <label class="f"><span>Advocate</span><input class="p-adv"></label>
    </div>
    <div class="row">
      <label class="f"><span>Email</span><input class="p-email"></label>
      <label class="f"><span>Phone</span><input class="p-phone"></label>
      <label class="inline" style="align-self:center"><input type="checkbox" class="p-cust"> in custody</label>
    </div>`;
  div.querySelector(".x").onclick = () => div.remove();
  if (p.side) div.querySelector(".p-side").value = p.side;
  if (p.name) div.querySelector(".p-name").value = p.name;
  if (p.advocate) div.querySelector(".p-adv").value = p.advocate;
  if (p.email) div.querySelector(".p-email").value = p.email;
  if (p.phone) div.querySelector(".p-phone").value = p.phone;
  $("nf-parties").appendChild(div);
}
$("nf-addparty").onclick = () => partyRow();

function readParties() {
  return Array.from($("nf-parties").children).map((d) => ({
    side: d.querySelector(".p-side").value,
    name: d.querySelector(".p-name").value.trim(),
    advocate: d.querySelector(".p-adv").value.trim() || null,
    email: d.querySelector(".p-email").value.trim() || null,
    phone: d.querySelector(".p-phone").value.trim() || null,
    in_custody: d.querySelector(".p-cust").checked,
  })).filter((p) => p.name);
}

const DOC_ROLES = ["petition", "memorandum_of_appeal", "certified_copy_of_judgment",
                   "vakalatnama", "affidavit", "annexure", "court_fee_receipt"];

async function loadDocPicker() {
  const r = await api("GET", "/api/documents?limit=60");
  if (!r.ok) { $("nf-docs").innerHTML = problem(r); return; }
  if (!r.data.length) { $("nf-docs").innerHTML = '<div class="empty">No documents yet. Upload some on the Documents tab.</div>'; return; }
  $("nf-docs").innerHTML = r.data.map((d) => `<div class="d">
    <input type="checkbox" value="${esc(d.document_id || d.id)}">
    <span class="t" title="${esc(d.title)}">${esc(d.title)}</span>
    <select>${DOC_ROLES.map((x) => `<option ${x === d.doc_type ? "selected" : ""}>${esc(x)}</option>`).join("")}</select>
  </div>`).join("");
}

function readDocs() {
  return Array.from($("nf-docs").querySelectorAll(".d"))
    .filter((d) => d.querySelector("input").checked)
    .map((d) => ({ document_id: d.querySelector("input").value, role: d.querySelector("select").value }));
}

function filingBody() {
  return {
    case_type: $("nf-type").value,
    title: $("nf-title").value.trim(),
    jurisdiction: $("nf-jur").value,
    claim_value: $("nf-claim").value ? Number($("nf-claim").value) : null,
    cause_of_action_date: $("nf-coa").value || null,
    filing_date: $("nf-date").value || null,
    exemptions: checked($("nf-ex")),
    parties: readParties(),
    documents: readDocs(),
  };
}

function objectionTable(objs) {
  return "<table><tr><th>objection</th><th>what is wrong</th><th>remedy</th></tr>" +
    objs.map((o) => `<tr><td><span class="tag bad">${esc(o.code)}</span></td>
      <td>${esc(o.message)}</td><td style="color:var(--muted)">${esc(o.remedy || "")}</td></tr>`).join("") +
    "</table>";
}

$("nf-validate").onclick = busy($("nf-validate"), async () => {
  const r = await api("POST", "/api/filing/validate", { body: filingBody() });
  if (!r.ok) { $("nf-out").innerHTML = problem(r); return; }
  $("nf-out").innerHTML = r.data.clean
    ? msg("ok", "Clean. Scrutiny found nothing to object to; submitting will allocate a case number.")
    : msg("bad", `${r.data.objections.length} objection(s), all reported at once &mdash; four defects should mean one resubmission, not four.`) +
      objectionTable(r.data.objections);
});

$("nf-submit").onclick = busy($("nf-submit"), async () => {
  const r = await api("POST", "/api/filing", { body: filingBody() });
  if (!r.ok) { $("nf-out").innerHTML = problem(r); return; }
  const d = r.data;
  if (d.status === "accepted") {
    $("nf-out").innerHTML = msg("ok",
      `Accepted as <strong class="mono">${esc(d.case_number)}</strong>, fee ${d.fee ? d.fee.total.toFixed(2) + " " + d.fee.currency : "-"}`) +
      "<table><tr><th>party</th><th>channel</th><th>destination</th><th>status</th></tr>" +
      d.confirmations.map((c) => `<tr><td>${esc(c.party)}</td><td>${esc(c.channel || "-")}</td>
        <td class="mono">${esc(c.destination || "-")}</td>
        <td><span class="tag ${c.status === "queued" ? "ok" : "warn"}">${esc(c.status)}</span></td></tr>`).join("") +
      "</table>" + msg("warn", "Confirmations are queued, not sent: no mail or SMS transport is wired.");
    loadCases(); loadCaseTypes(); loadCaseSelects();
  } else {
    $("nf-out").innerHTML = msg("bad", `Rejected, and <strong>no case number was allocated</strong> &mdash; a rejected filing leaves no gap in the register.`) +
      objectionTable(d.objections) +
      msg("warn", "The attempt is still recorded with its objections and its date. Fix the objections and submit again.");
    loadCaseTypes();
  }
});

async function loadCases() {
  const r = await api("GET", "/api/filing?limit=50");
  if (!r.ok) { $("cs-out").innerHTML = problem(r); return; }
  if (!r.data.cases.length) { $("cs-out").innerHTML = '<div class="empty">No cases on the register yet.</div>'; return; }
  $("cs-out").innerHTML = `<div class="scroll"><table>
    <tr><th>number</th><th>type</th><th>title</th><th>status</th><th>filed</th></tr>` +
    r.data.cases.map((c) => `<tr class="click" data-case="${esc(c.id)}">
      <td class="mono"><strong>${esc(c.case_number)}</strong></td><td>${esc(c.case_type)}</td>
      <td>${esc(c.title)}</td><td><span class="tag neutral">${esc(c.status)}</span></td>
      <td>${esc((c.filed_at || "").slice(0, 10))}</td></tr>`).join("") +
    `</table></div><div class="out" id="cs-detail"></div>`;
  $("cs-out").querySelectorAll("tr[data-case]").forEach((tr) => {
    tr.onclick = async () => {
      const t = await api("GET", `/api/filing/cases/${tr.dataset.case}`);
      if (!t.ok) { $("cs-detail").innerHTML = problem(t); return; }
      const c = t.data;
      $("cs-detail").innerHTML = msg("ok", `<strong class="mono">${esc(c.case_number)}</strong> &mdash; ${esc(c.title)}`) +
        "<table><tr><th>side</th><th>party</th><th>advocate</th><th>contact</th></tr>" +
        c.parties.map((p) => `<tr><td>${esc(p.side)}</td><td>${esc(p.name)}</td>
          <td>${esc(p.advocate || "-")}</td><td class="mono">${esc(p.email || p.phone || "none on record")}</td></tr>`).join("") +
        "</table>" +
        (c.hearings.length
          ? "<table style='margin-top:10px'><tr><th>hearing</th><th>judge</th><th>hall</th><th>status</th><th>id</th></tr>" +
            c.hearings.map((h) => `<tr><td class="mono">${esc(h.starts_at)}</td><td>${esc(h.judge_name)}</td>
              <td>${esc(h.courtroom_name)}</td><td><span class="tag neutral">${esc(h.status)}</span></td>
              <td class="mono">${esc(h.id.slice(0, 8))}…</td></tr>`).join("") + "</table>"
          : '<div class="empty">No hearings scheduled. Use the Scheduling tab.</div>');
    };
  });
}
$("cs-reload").onclick = busy($("cs-reload"), loadCases);

// ===================================================================
// SCHEDULING
// ===================================================================
async function loadResources() {
  const r = await api("GET", "/api/scheduling/resources");
  if (!r.ok) { $("rs-out").innerHTML = problem(r); return; }
  const d = r.data;
  $("rs-out").innerHTML =
    "<table><tr><th>judge</th><th>sits</th></tr>" +
    d.judges.map((j) => `<tr><td>${esc(j.name)} <span class="mono" style="color:var(--faint)">${esc(j.id)}</span></td>
      <td class="mono">${esc(j.sitting_start)}&ndash;${esc(j.sitting_end)}</td></tr>`).join("") + "</table>" +
    "<table style='margin-top:10px'><tr><th>courtroom</th><th>capacity</th><th>video link</th></tr>" +
    d.courtrooms.map((c) => `<tr><td>${esc(c.name)} <span class="mono" style="color:var(--faint)">${esc(c.id)}</span></td>
      <td>${esc(c.capacity)}</td>
      <td><span class="tag ${c.video_link ? "ok" : "neutral"}">${c.video_link ? "yes" : "no"}</span></td></tr>`).join("") + "</table>" +
    "<table style='margin-top:10px'><tr><th>holiday</th><th>reason</th></tr>" +
    d.holidays.map((h) => `<tr><td class="mono">${esc(h.day)}</td><td>${esc(h.reason || "")}</td></tr>`).join("") + "</table>";

  const jopts = d.judges.map((j) => `<option value="${esc(j.id)}">${esc(j.name)}</option>`).join("");
  const ropts = d.courtrooms.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}${c.video_link ? " (video)" : ""}</option>`).join("");
  $("av-judge").innerHTML = jopts; $("sh-judge").innerHTML = jopts;
  $("av-room").innerHTML = ropts; $("sh-room").innerHTML = ropts;
}
$("rs-reload").onclick = busy($("rs-reload"), loadResources);

async function loadCaseSelects() {
  const r = await api("GET", "/api/filing?limit=50");
  if (!r.ok) return;
  const opts = r.data.cases.map((c) =>
    `<option value="${esc(c.id)}">${esc(c.case_number)} — ${esc(c.title.slice(0, 40))}</option>`).join("");
  $("av-case").innerHTML = opts || "<option value=''>(no cases filed yet)</option>";
  $("sh-case").innerHTML = opts || "<option value=''>(no cases filed yet)</option>";
}

$("av-go").onclick = busy($("av-go"), async () => {
  const qs = new URLSearchParams({
    case_id: $("av-case").value, judge_id: $("av-judge").value,
    courtroom_id: $("av-room").value, day: $("av-day").value,
    duration_minutes: $("av-dur").value || 30,
    requires_video: $("av-video").checked,
  });
  const r = await api("GET", `/api/scheduling/availability?${qs}`);
  if (!r.ok) { $("av-out").innerHTML = problem(r); return; }
  const d = r.data;
  if (!d.slots.length) {
    $("av-out").innerHTML = msg("warn", "No slots at all. That judge may not sit, or the day is a holiday.");
    return;
  }
  $("av-out").innerHTML = msg(d.free_count ? "ok" : "bad",
    `${d.free_count} of ${d.total_slots} slots free`) +
    `<div class="slots">${d.slots.map((s) => `
      <div class="slot ${s.free ? "free" : "busy"}"
           data-s="${esc(s.starts_at)}" data-e="${esc(s.ends_at)}"
           title="${s.free ? "click to load into the form" : "blocked by: " + (s.blocked_by || []).join(", ")}">
        ${esc(s.starts_at.slice(11, 16))}</div>`).join("")}</div>` +
    (d.free_count < d.total_slots
      ? msg("warn", "Struck-through slots are blocked. Hover one to see which check refused it.") : "");
  $("av-out").querySelectorAll(".slot.free").forEach((el) => {
    el.onclick = () => {
      $("sh-start").value = el.dataset.s; $("sh-end").value = el.dataset.e;
      $("sh-case").value = $("av-case").value;
      $("sh-judge").value = $("av-judge").value; $("sh-room").value = $("av-room").value;
      $("sh-out").innerHTML = msg("ok", `Loaded ${esc(el.dataset.s)} into the scheduling form.`);
    };
  });
});

$("sh-go").onclick = busy($("sh-go"), async () => {
  const r = await api("POST", "/api/scheduling/hearings", {
    body: {
      case_id: $("sh-case").value, judge_id: $("sh-judge").value,
      courtroom_id: $("sh-room").value, starts_at: $("sh-start").value,
      ends_at: $("sh-end").value, purpose: $("sh-purpose").value,
    },
  });
  if (!r.ok) {
    $("sh-out").innerHTML = problem(r) + (r.status === 409
      ? msg("warn", "The slot is refused, not the request. An advocate conflict is checked across courts on purpose: the point is to catch counsel listed in two courts at once.")
      : "");
    return;
  }
  const d = r.data;
  $("sh-out").innerHTML = msg("ok", `Listed. Hearing id <code>${esc(d.hearing_id)}</code>`) +
    (d.reminders && d.reminders.length
      ? "<table><tr><th>channel</th><th>destination</th><th>status</th></tr>" +
        d.reminders.map((x) => `<tr><td>${esc(x.channel || "-")}</td>
          <td class="mono">${esc(x.destination || "-")}</td>
          <td><span class="tag ok">${esc(x.status)}</span></td></tr>`).join("") + "</table>"
      : "");
  $("aj-id").value = d.hearing_id;
  loadCases();
});

$("cl-go").onclick = busy($("cl-go"), async () => {
  const r = await api("GET", `/api/scheduling/cause-list?day=${$("cl-day").value}`);
  if (!r.ok) { $("cl-out").innerHTML = problem(r); return; }
  const d = r.data;
  if (!d.item_count) { $("cl-out").innerHTML = msg("warn", `Nothing listed in ${esc(d.court_id)} on ${esc(d.day)}.`); return; }
  $("cl-out").innerHTML = msg("ok", `${esc(d.court_id)} &mdash; ${esc(d.day)} &mdash; ${d.item_count} item(s)`) +
    `<div class="scroll"><table><tr><th>#</th><th>time</th><th>case</th><th>before</th><th>hall</th><th>advocates</th></tr>` +
    d.items.map((i) => `<tr><td>${i.item_no}</td><td class="mono">${esc(i.starts_at.slice(11, 16))}</td>
      <td class="mono">${esc(i.case_number)}<div style="color:var(--muted)">${esc(i.title)}</div></td>
      <td>${esc(i.judge)}</td><td>${esc(i.courtroom)}</td>
      <td>${(i.advocates || []).map((a) => esc(a)).join("<br>")}</td></tr>`).join("") + "</table></div>";
});

$("rm-go").onclick = busy($("rm-go"), async () => {
  const r = await api("GET", "/api/scheduling/reminders/due");
  if (!r.ok) { $("rm-out").innerHTML = problem(r); return; }
  const list = r.data.reminders || r.data;
  if (!list.length) { $("rm-out").innerHTML = msg("warn", "Nothing due. Reminders are queued a few days before a hearing."); return; }
  $("rm-out").innerHTML = msg("ok", `${list.length} reminder(s) a dispatch worker would pick up`) +
    "<table><tr><th>channel</th><th>destination</th><th>case</th><th>send at</th><th>hearing</th></tr>" +
    list.map((x) => `<tr><td>${esc(x.channel)}</td><td class="mono">${esc(x.destination)}</td>
      <td class="mono">${esc(x.case_number || "")}</td>
      <td class="mono">${esc((x.scheduled_for || "").slice(0, 16))}</td>
      <td class="mono">${esc((x.hearing_at || "").slice(0, 16))}</td></tr>`).join("") +
    "</table>" + msg("warn", "Queued only. No mail or SMS transport is wired.");
});

$("aj-go").onclick = busy($("aj-go"), async () => {
  const id = $("aj-id").value.trim();
  if (!id) { $("aj-out").innerHTML = msg("warn", "Paste a hearing id."); return; }
  const r = await api("POST", `/api/scheduling/hearings/${id}/adjourn`, { body: { reason: $("aj-reason").value } });
  $("aj-out").innerHTML = r.ok
    ? msg("ok", "Adjourned. The slot is free again and this hearing's queued reminders are skipped.") + json(r.data)
    : problem(r);
  if (r.ok) loadCases();
});

// ===================================================================
// INTEGRATION HUB
// ===================================================================
const SAMPLES = {
  "prison:custody_status": { party_id: "p-123" },
  "prison:produce_warrant": { party_id: "p-123", produce_on: "2026-11-20T11:00:00" },
  "payments:collect": { amount: 975.0, currency: "INR" },
  "payments:refund": { receipt: "RCPT-0000000000", amount: 975.0 },
  "identity:verify": { identity_number: "1234-5678-9012", name: "Ramesh Kumar" },
  "legal_aid:check_eligibility": { annual_income: 90000 },
  "legal_aid:assign_counsel": { case_id: "", party_id: "p-123" },
  "bar_directory:verify_enrolment": { enrolment_number: "KAR/1234/2009", name: "Sri. Vinay Hegde" },
};
let connectors = [];

async function loadConnectors() {
  const r = await api("GET", "/api/integrations");
  if (!r.ok) { $("ic-out").innerHTML = problem(r); return; }
  connectors = r.data.connectors;
  $("ic-out").innerHTML = `<div class="scroll"><table>
    <tr><th>connector</th><th>operations</th><th>circuit</th><th>fails</th><th>roles permitted</th></tr>` +
    connectors.map((c) => `<tr><td><strong>${esc(c.name)}</strong>
        <div class="mono" style="color:var(--faint)">${esc(c.connector_id)} &middot; ${esc(c.implementation)}</div></td>
      <td>${c.operations.map((o) => `<span class="tag neutral">${esc(o)}</span>`).join(" ")}</td>
      <td><span class="tag ${c.circuit === "closed" ? "ok" : "bad"}">${esc(c.circuit)}</span></td>
      <td>${c.consecutive_fails}</td>
      <td style="color:var(--muted)">${c.allowed_roles.join(", ")}</td></tr>`).join("") + "</table></div>" +
    msg("warn", esc(r.data.implementation_note));
  if (!$("iv-conn").options.length) {
    $("iv-conn").innerHTML = connectors.map((c) => `<option value="${esc(c.connector_id)}">${esc(c.name)}</option>`).join("");
    syncOps();
  }
}
$("ic-reload").onclick = busy($("ic-reload"), loadConnectors);

function syncOps() {
  const c = connectors.find((x) => x.connector_id === $("iv-conn").value);
  if (!c) return;
  $("iv-op").innerHTML = c.operations.map((o) => `<option>${esc(o)}</option>`).join("");
  syncPayload();
}
function syncPayload() {
  const key = `${$("iv-conn").value}:${$("iv-op").value}`;
  $("iv-payload").value = JSON.stringify(SAMPLES[key] || {}, null, 2);
}
$("iv-conn").onchange = syncOps;
$("iv-op").onchange = syncPayload;
$("iv-newkey").onclick = () => { $("iv-idem").value = "ui-" + crypto.randomUUID(); };

async function invoke() {
  let payload;
  try { payload = JSON.parse($("iv-payload").value || "{}"); }
  catch (e) { $("iv-out").innerHTML = msg("bad", "Payload is not valid JSON."); return; }
  const body = { operation: $("iv-op").value, payload };
  if ($("iv-fail").checked) body._fail_injection = true;
  const key = $("iv-idem").value.trim();
  const r = await api("POST", `/api/integrations/${$("iv-conn").value}/invoke`,
                      { body, headers: key ? { "Idempotency-Key": key } : {} });
  if (!r.ok) {
    $("iv-out").innerHTML = problem(r) + msg("warn",
      r.status === 503 ? "503 means the breaker is open: three consecutive failures, so calls now fail fast instead of queueing behind a dead upstream. It closes after two minutes."
      : r.status === 502 ? "502 is the induced upstream failure. Two more and the circuit opens."
      : r.status === 403 ? "This role may not use this connector. A clerk settling a fee is ordinary; a clerk querying the identity register is not."
      : "");
    loadConnectors();
    return;
  }
  $("iv-out").innerHTML = msg("ok",
    r.data.replayed
      ? "<strong>Replayed</strong> from the idempotency record. The adapter was not called again, so nothing was charged twice."
      : "Called. Note <code>\"mock\": true</code> in the response: this is synthetic data, not an upstream answer.") +
    json(r.data);
  loadConnectors();
}
$("iv-go").onclick = busy($("iv-go"), async () => {
  if (!$("iv-idem").value.trim()) $("iv-idem").value = "ui-" + crypto.randomUUID();
  await invoke();
});
$("iv-again").onclick = busy($("iv-again"), invoke);

$("il-go").onclick = busy($("il-go"), async () => {
  const r = await api("GET", "/api/integrations/calls?limit=40");
  if (!r.ok) {
    $("il-out").innerHTML = problem(r) + msg("warn", "Judge or registrar only. Switch role above.");
    return;
  }
  $("il-out").innerHTML = `<div class="scroll"><table>
    <tr><th>connector</th><th>operation</th><th>status</th><th>tries</th><th>ms</th><th>request (redacted)</th></tr>` +
    r.data.calls.map((c) => `<tr><td>${esc(c.connector_id)}</td><td>${esc(c.operation)}</td>
      <td><span class="tag ${c.status === "ok" ? "ok" : "bad"}">${esc(c.status)}</span></td>
      <td>${esc(c.attempts)}</td><td>${esc(c.duration_ms)}</td>
      <td class="mono">${esc(c.request_json)}</td></tr>`).join("") + "</table></div>" +
    msg("ok", esc(r.data.note));
});

// ===================================================================
// boot
// ===================================================================
(async function boot() {
  $("fe-date").value = today();
  $("nf-date").value = today();
  $("nf-coa").value = today(-30);
  $("av-day").value = today(7);
  $("cl-day").value = today(7);
  partyRow({ side: "petitioner", name: "Ramesh Kumar", advocate: "Sri. Vinay Hegde", email: "ramesh@example.in" });
  partyRow({ side: "respondent", name: "State of Karnataka", advocate: "Smt. Deepa Shetty", email: "state@example.in" });
  await health();
  await loadCaseTypes();
  await loadResources();
  await loadCaseSelects();
  await loadCases();
  await loadDocPicker();
  await loadConnectors();
  $("s-all").click();
})();

// Re-read anything role-dependent when the principal changes.
["p-user", "p-role", "p-jur"].forEach((id) => {
  $(id).onchange = () => { loadCases(); loadDocPicker(); $("s-all").click(); };
});
