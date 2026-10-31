"""
Browser end-to-end tests: the real page in headless Chromium, against a fake
YNAB (fake_ynab.py) and made-up statement PDFs (make_pdf.py).

    test_real_pdf.py  - a generated PDF through real pdfplumber + parsers/gcash.py
    test_web_flow.py  - the 4 steps of the web page, driven with Playwright;
                        skipped without Playwright unless REQUIRE_E2E=1

Reproduce a CI failure by hand:  python -m tests.e2e.fake_server
"""
