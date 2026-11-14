#!/usr/bin/env python3
"""Drive the browser UI at /ui and fail on any console error.

    ./.venv/bin/python test_ui.py              # needs the server on :8020
    ./.venv/bin/python test_ui.py --headed     # watch it run

A page that renders but reads the wrong JSON key looks fine in a screenshot
and is useless in practice, so this clicks through every tab and asserts on
what actually appears in the DOM.
"""
from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8020"
HEADED = "--headed" in sys.argv

problems: list[str] = []
checks = 0


def ok(label: str, condition: bool, detail: str = "") -> None:
    global checks
    checks += 1
    mark = "\033[32m✓\033[0m" if condition else "\033[31m✗\033[0m"
    print(f"  {mark} {label}" + (f"  [{detail}]" if detail and not condition else ""))
    if not condition:
        problems.append(f"{label} {detail}".strip())


def hr(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


def run() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not HEADED)
        page = browser.new_page(viewport={"width": 1500, "height": 1000})

        console: list[str] = []
        def on_console(m):
            if m.type not in ("error", "warning"):
                return
            # "Failed to load resource" is Chrome reporting a deliberate 403/409.
            if "Failed to load resource" in m.text:
                return
            console.append(f"{m.type}: {m.text}")

        page.on("console", on_console)
        page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))

        hr("0  Load the page")
        page.goto(f"{BASE}/ui", wait_until="networkidle")
        ok("title", "Court Case Management" in page.title(), page.title())
        ok("health pill shows 4/4 features",
           "4/4" in page.inner_text("#h-health"), page.inner_text("#h-health"))
        ok("Landing AI reported configured",
           "configured" in page.inner_text("#h-ade") and "NOT" not in page.inner_text("#h-ade"),
           page.inner_text("#h-ade"))

        hr("1  Documents tab")
        page.click("#s-all")
        page.wait_for_timeout(700)
        rows = page.locator("#s-out tr[data-doc]").count()
        ok("document list rendered", rows > 0, f"{rows} rows")
        if rows:
            page.locator("#s-out tr[data-doc]").first.click()
            page.wait_for_timeout(900)
            ok("detail pane shows a version table",
               page.locator("#d-out table").count() > 0)
            ok("version row has a hash", "…" in page.inner_text("#d-out"))

        page.fill("#s-q", "quash proceedings")
        page.click("#s-go")
        page.wait_for_timeout(900)
        ok("search returned hits", "hit(s)" in page.inner_text("#s-out"),
           page.inner_text("#s-out")[:90])

        hr("2  Role switching changes what the API returns")
        page.select_option("#p-role", "public")
        page.wait_for_timeout(1100)
        pub = page.locator("#s-out tr[data-doc]").count()
        ok("public sees fewer documents than clerk", pub < rows, f"public={pub} clerk={rows}")
        page.select_option("#p-role", "judge")
        page.wait_for_timeout(1100)

        hr("3  Audit trail, judge only")
        if page.locator("#s-out tr[data-doc]").count():
            page.locator("#s-out tr[data-doc]").first.click()
            page.wait_for_timeout(900)
            page.click("#d-audit")
            page.wait_for_timeout(700)
            txt = page.inner_text("#d-sub")
            ok("audit entries rendered with actor ids", "ALLOWED" in txt or "REFUSED" in txt,
               txt[:90])

        hr("4  Case filing tab")
        page.click('nav button[data-tab="filing"]')
        page.wait_for_timeout(400)
        ok("case types table rendered", page.locator("#ct-out tr").count() > 1,
           f"{page.locator('#ct-out tr').count()} rows")
        ok("next case number shown", "/" in page.inner_text("#ct-out"))

        page.select_option("#fe-type", "O.S")
        page.fill("#fe-claim", "750000")
        page.click("#fe-go")
        page.wait_for_timeout(700)
        fee = page.inner_text("#fe-out")
        ok("fee total rendered", "INR" in fee, fee[:90])
        ok("ad valorem slabs shown", "%" in fee)
        ok("placeholder disclaimer shown", "placeholder" in fee.lower(), fee[-120:])

        page.select_option("#nf-type", "CRL.A")
        page.fill("#nf-title", "UI test - Ramesh Kumar v. State")
        page.click("#nf-validate")
        page.wait_for_timeout(800)
        vtxt = page.inner_text("#nf-out")
        ok("validation reported objections (nothing attached yet)",
           "objection" in vtxt.lower(), vtxt[:110])
        ok("objection codes rendered", "missing_document" in vtxt, vtxt[:160])

        # Attach the three documents a CRL.A needs, then file it for real.
        boxes = page.locator("#nf-docs .d")
        needed = ["memorandum_of_appeal", "certified_copy_of_judgment", "vakalatnama"]
        n = min(boxes.count(), 3)
        for i in range(n):
            boxes.nth(i).locator("input").check()
            boxes.nth(i).locator("select").select_option(needed[i])
        page.fill("#nf-coa", "2026-10-20")      # inside the 90-day limitation
        page.click("#nf-submit")
        page.wait_for_timeout(1200)
        stxt = page.inner_text("#nf-out")
        filed = "Accepted as" in stxt
        ok("filing accepted and numbered", filed, stxt[:160])
        if filed:
            ok("confirmations queued, not sent", "queued" in stxt)

        page.click("#cs-reload")
        page.wait_for_timeout(700)
        ok("register lists cases", page.locator("#cs-out tr[data-case]").count() > 0)
        page.locator("#cs-out tr[data-case]").first.click()
        page.wait_for_timeout(700)
        ok("case detail shows parties", "petitioner" in page.inner_text("#cs-detail").lower(),
           page.inner_text("#cs-detail")[:90])

        hr("5  Scheduling tab")
        page.click('nav button[data-tab="scheduling"]')
        page.wait_for_timeout(500)
        ok("judges and halls rendered", "Justice" in page.inner_text("#rs-out"),
           page.inner_text("#rs-out")[:90])
        ok("video-link column present", "video" in page.inner_text("#rs-out").lower())

        page.click("#av-go")
        page.wait_for_timeout(1500)
        free = page.locator("#av-out .slot.free").count()
        busy_slots = page.locator("#av-out .slot.busy").count()
        ok("availability grid rendered", free + busy_slots > 0, f"{free} free, {busy_slots} blocked")
        if free:
            page.locator("#av-out .slot.free").first.click()
            page.wait_for_timeout(300)
            ok("clicking a free slot fills the form", page.input_value("#sh-start") != "")
            page.click("#sh-go")
            page.wait_for_timeout(1400)
            sh = page.inner_text("#sh-out")
            ok("hearing scheduled", "Listed" in sh, sh[:140])
            if "Listed" in sh:
                page.click("#sh-go")          # same slot twice
                page.wait_for_timeout(1200)
                conf = page.inner_text("#sh-out")
                ok("re-booking the same slot is refused with conflicts",
                   "judge_busy" in conf or "courtroom_busy" in conf, conf[:160])

        page.click("#cl-go")
        page.wait_for_timeout(900)
        ok("cause list rendered", "item(s)" in page.inner_text("#cl-out"),
           page.inner_text("#cl-out")[:90])
        page.click("#rm-go")
        page.wait_for_timeout(800)
        ok("reminder queue rendered", "reminder" in page.inner_text("#rm-out").lower(),
           page.inner_text("#rm-out")[:90])

        hr("6  Integration hub tab")
        page.click('nav button[data-tab="integrations"]')
        page.wait_for_timeout(600)
        ic = page.inner_text("#ic-out")
        ok("five connectors listed", page.locator("#ic-out tr").count() >= 6,
           f"{page.locator('#ic-out tr').count()} rows")
        ok("all marked mock", ic.count("mock") >= 5)
        ok("circuit state shown", "closed" in ic or "open" in ic)

        page.select_option("#p-role", "clerk")   # a judge may not settle fees
        page.wait_for_timeout(600)
        page.select_option("#iv-conn", "payments")
        page.wait_for_timeout(300)
        page.click("#iv-go")
        page.wait_for_timeout(1200)
        iv = page.inner_text("#iv-out")
        ok("payment collected", "mock" in iv, iv[:120])
        page.click("#iv-again")          # same idempotency key
        page.wait_for_timeout(1200)
        iv2 = page.inner_text("#iv-out")
        ok("same key replays instead of charging twice", "Replayed" in iv2, iv2[:140])

        page.select_option("#p-role", "litigant")
        page.wait_for_timeout(600)
        page.select_option("#iv-conn", "identity")
        page.wait_for_timeout(300)
        page.click("#iv-go")
        page.wait_for_timeout(1000)
        ok("litigant refused the identity register",
           "403" in page.inner_text("#iv-out"), page.inner_text("#iv-out")[:120])

        page.select_option("#p-role", "registrar")
        page.wait_for_timeout(600)
        page.click("#il-go")
        page.wait_for_timeout(900)
        log = page.inner_text("#il-out")
        ok("call log readable by registrar", "payments" in log, log[:120])
        ok("identity number redacted in the log", "redacted" in log, log[:200])

        hr("7  Request log drawer")
        page.click("#log-head")
        page.wait_for_timeout(300)
        ok("drawer logged every call", page.locator("#log-body .r").count() > 20,
           f"{page.locator('#log-body .r').count()} rows")
        ok("non-2xx responses visible in the drawer",
           page.locator("#log-body .s4").count() > 0,
           f"{page.locator('#log-body .s4').count()} 4xx rows")

        page.screenshot(path="/tmp/ui_integrations.png", full_page=True)
        page.click('nav button[data-tab="documents"]')
        page.wait_for_timeout(400)
        page.screenshot(path="/tmp/ui_documents.png", full_page=True)

        hr("8  Browser console")
        real = [c for c in console if "favicon" not in c.lower()]
        ok("no console errors", not real, "; ".join(real[:4]))

        browser.close()


run()
print(f"\n\033[1m{checks - len(problems)}/{checks} checks passed\033[0m")
if problems:
    print("\033[31mFAILURES:\033[0m")
    for p in problems:
        print(f"  - {p}")
    sys.exit(1)
print("\033[32mUI verified end to end.\033[0m")
