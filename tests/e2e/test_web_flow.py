"""
The web page, driven the way a user would in headless Chromium (Playwright),
against the fake YNAB (fake_ynab.py) and made-up statement PDFs (make_pdf.py).

Each test gets a fresh browser context (new session cookie) and a fresh fake
YNAB. Elements are found by the page's own ids (#pdf, #parse-btn, #ynab-token,
#account, #upload-btn, ...).

On a failure a screenshot and a Playwright trace go to test-results/
(open the trace with: python -m playwright show-trace <file>.zip).

Skipped when Playwright isn't installed. With REQUIRE_E2E=1 (set in CI) a
missing Playwright or Chromium is an error instead.

Setup:
    pip install -r requirements-dev.txt
    python -m playwright install chromium
    REQUIRE_E2E=1 python -m unittest tests.e2e.test_web_flow -v
"""

import os
import re
import tempfile
import unittest
import urllib.parse
from pathlib import Path

import tests  # noqa: F401 - the safety net: nothing reaches YNAB or the keyring
from tests.e2e import fake_ynab, make_pdf
from tests.e2e.fake_server import FakeServer
from tests.hosted import PUBLIC_URL, hosted_mode

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    sync_playwright = None

REQUIRE_E2E = os.environ.get("REQUIRE_E2E") == "1"
RESULTS_DIR = Path(__file__).resolve().parents[2] / "test-results"
TIMEOUT_MS = 10_000

# Made up; the generated PDFs aren't encrypted, so any password opens them
PDF_PASSWORD = "not-a-real-password"


@unittest.skipIf(
    sync_playwright is None and not REQUIRE_E2E,
    "Playwright not installed (pip install -r requirements-dev.txt)",
)
class BrowserTest(unittest.TestCase):
    """Headless Chromium on the web app with the fake YNAB; hosted = True for hosted mode."""

    hosted = False

    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise RuntimeError("REQUIRE_E2E=1 but Playwright is not installed")
        if cls.hosted:
            cls.enterClassContext(hosted_mode())  # before the server takes the app
        cls.server = FakeServer().start()
        cls.addClassCleanup(cls.server.stop)
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        try:
            cls.browser = cls.playwright.chromium.launch()
        except PlaywrightError as e:
            if REQUIRE_E2E:
                raise
            raise unittest.SkipTest(
                f"Chromium not installed (python -m playwright install chromium): {e}"
            ) from None
        cls.addClassCleanup(cls.browser.close)
        expect.set_options(timeout=TIMEOUT_MS)

    def setUp(self):
        self.server.reset()
        self.ynab = self.server.ynab
        self.context = self.browser.new_context()
        self.context.set_default_timeout(TIMEOUT_MS)
        self.context.tracing.start(screenshots=True, snapshots=True)
        self.page = self.context.new_page()
        # Errors in the browser console, e.g. code the Content-Security-Policy blocked
        self.console_errors = []
        self.page.on("console", lambda m: m.type == "error" and self.console_errors.append(m.text))
        # networkidle: the page's start-up /api/status call has finished, so it
        # can't undo what the test does next
        self.page.goto(self.server.url, wait_until="networkidle")

    def tearDown(self):
        # unittest marks the outcome before tearDown runs
        failed = not getattr(self._outcome, "success", True)
        name = re.sub(r"[^\w.-]", "_", self.id())
        try:
            if failed:
                RESULTS_DIR.mkdir(exist_ok=True)
                self.page.screenshot(path=RESULTS_DIR / f"{name}.png", full_page=True)
                self.context.tracing.stop(path=RESULTS_DIR / f"{name}-trace.zip")
            else:
                self.context.tracing.stop()
        finally:
            self.context.close()


class WebFlowTest(BrowserTest):
    """Local mode: the 4 steps with a personal access token."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        folder = Path(cls.tmp.name)
        cls.balanced_pdf = folder / "balanced.pdf"
        cls.gap_pdf = folder / "with_gap.pdf"
        cls.extended_pdf = folder / "extended.pdf"
        make_pdf.balanced(cls.balanced_pdf)
        make_pdf.with_gap(cls.gap_pdf)
        # The balanced statement plus one later row, as the next month's
        # statement would overlap the last one
        make_pdf.make_statement(
            cls.extended_pdf,
            make_pdf.BALANCED_ROWS
            + [
                make_pdf.Row(
                    "2025-09-06 07:45 PM",
                    "Payment to Sample Bakery",
                    "9990000000005",
                    "50.00",
                    "",
                    "1200.00",
                )
            ],
            make_pdf.BALANCED_STARTING,
            "1200.00",
        )
        super().setUpClass()

    # ---- user actions ----------------------------------------------------------

    def parse(self, pdf, password=PDF_PASSWORD):
        """Step 1: pick GCash, choose the PDF, type the password, Parse."""
        page = self.page
        page.select_option("#source", "gcash")
        page.set_input_files("#pdf", str(pdf))
        page.fill("#password", password)
        page.click("#parse-btn")
        expect(page.locator("#parse-btn")).to_have_text("Parse")

    def connect(self, token=fake_ynab.TOKEN):
        """Step 2: type the personal access token, Connect."""
        self.page.fill("#ynab-token", token)
        self.page.click("#token-btn")
        expect(self.page.locator("#token-btn")).to_have_text("Connect")

    def choose_account(self, account_id):
        """Step 3: the only plan is picked by itself; choose the account."""
        expect(self.page.locator("#plan")).to_have_value(fake_ynab.PLAN_ID)
        self.page.select_option("#account", account_id)

    def upload(self):
        """Step 4: Upload, and wait until it's done."""
        self.page.click("#upload-btn")
        expect(self.page.locator("#upload-btn")).not_to_have_text("Uploading…")

    def preview_rows(self):
        return self.page.locator("#preview-table tbody tr")

    def result_rows(self):
        return self.page.locator("#results-table tbody tr")

    def created(self, account_id):
        """Transactions the app created in the fake YNAB account."""
        return [t for t in self.ynab.transactions(account_id) if t.import_id]

    # ---- scenarios ---------------------------------------------------------------

    def test_happy_path_through_all_four_steps(self):
        page = self.page
        account = fake_ynab.EXISTING_ACCOUNT_ID

        # 1. Statement
        self.parse(self.balanced_pdf)
        expect(page.locator("#parse-msg")).to_have_text(
            "Parsing successful: 4 GCash transactions from balanced.pdf"
        )
        expect(self.preview_rows()).to_have_count(4)
        expect(self.preview_rows().nth(1)).to_contain_text(
            "Payment to Sample Coffee Shop, Merchant Transaction Number: TEST0000001"
        )
        expect(page.locator("#preview-totals")).to_have_text(
            "Total debit 500.00 · Total credit 750.00 · matches statement ✓"
        )
        expect(page.locator("#balance-msg")).to_be_hidden()
        expect(page.locator("#parse-steps")).to_be_hidden()  # the message above says it all
        expect(page.locator("#card-parse")).to_have_class(re.compile(r"\bcomplete\b"))

        # 2. YNAB
        self.connect()
        expect(page.locator("#status-pill")).to_have_text("● Connected")
        expect(page.locator("#ynab-on-label")).to_have_text(
            "● Connected with your personal access token"
        )
        expect(page.locator("#ynab-token")).to_have_value("")

        # 3. Plan & Account
        expect(page.locator("#account option")).to_have_count(3)  # "Choose…" + 2
        self.choose_account(account)
        expect(page.locator("#start-check")).to_have_text(
            "✓ Starting balance matches YNAB: 1,000.00"
        )

        # 4. Upload
        expect(page.locator("#upload-summary")).to_have_text(
            f"4 transactions → {fake_ynab.PLAN_NAME} / "
            f"{fake_ynab.EXISTING_ACCOUNT_NAME} (cash) – ₱1,000.00"
        )
        expect(page.locator("#upload-btn")).to_have_text("Upload 4 transactions to YNAB")
        expect(page.locator("#upload-btn")).to_be_enabled()
        self.upload()
        expect(page.locator("#upload-msg")).to_have_text(
            "Import successful: 4 transactions created in YNAB (unapproved)"
        )
        expect(self.result_rows()).to_have_count(4)
        expect(self.result_rows().nth(1)).to_contain_text("Sample Coffee Shop")
        expect(self.result_rows().nth(1)).to_contain_text("GCash Ref: 9990000000002")
        expect(self.result_rows().nth(1)).to_contain_text("-₱150.25")
        expect(page.locator("#results-table tbody tr.missing")).to_have_count(0)
        expect(page.locator("#end-check")).to_have_text("✓ Ending balance matches YNAB: 1,250.00")
        expect(page.locator("#upload-steps li")).to_have_text(
            [
                "Statement read: 4 rows",
                "Starting balance checked",
                "Uploaded 4 transactions to YNAB",
                "Ending balance checked",
            ]
        )
        expect(page.locator("#upload-steps li.done")).to_have_count(4)
        expect(page.locator("#upload-elapsed")).to_contain_text("Finished in")
        expect(page.locator("#ynab-link")).to_have_attribute(
            "href", f"https://app.ynab.com/{fake_ynab.PLAN_ID}/accounts/{account}"
        )
        expect(page.locator("#upload-skipped")).to_be_hidden()
        expect(page.locator("#upload-btn")).to_have_text("Nothing new to upload")
        expect(page.locator("#upload-btn")).to_be_disabled()

        # What the fake YNAB received
        created = self.created(account)
        self.assertEqual(len(created), 4)
        self.assertEqual(self.ynab.balance(account), 1_250_000)
        self.assertFalse(any(t.approved for t in created))
        self.assertEqual([t.amount for t in created], [500_000, -150_250, 250_000, -349_750])
        # Nothing on the page was blocked by the Content-Security-Policy
        self.assertEqual(self.console_errors, [])

    def test_reupload_skips_what_is_already_in_ynab(self):
        page = self.page
        account = fake_ynab.EXISTING_ACCOUNT_ID
        self.parse(self.balanced_pdf)
        self.connect()
        self.choose_account(account)
        self.upload()
        expect(page.locator("#upload-msg")).to_contain_text("4 transactions created")

        # The same statement again: nothing new to upload
        self.parse(self.balanced_pdf)
        start = page.locator("#start-check")
        expect(start).to_contain_text("4 transactions already in YNAB. Only 0 will be uploaded.")
        expect(page.locator("#upload-btn")).to_have_text("Nothing new to upload")
        expect(page.locator("#upload-btn")).to_be_disabled()

        # A later statement that overlaps it: only the new row is added
        self.parse(self.extended_pdf)
        expect(start).to_contain_text("4 transactions already in YNAB. Only 1 will be uploaded.")
        expect(start).to_contain_text("Starting balance does not match YNAB. You can still import.")
        expect(start.locator("dd")).to_have_text(["1,000.00", "1,250.00", "1,200.00"])
        expect(page.locator("#upload-btn")).to_have_text("Upload 1 transaction to YNAB")
        self.upload()
        expect(page.locator("#upload-msg")).to_have_text(
            "Import successful: 1 transactions created in YNAB (unapproved)"
        )
        expect(page.locator("#upload-skipped")).to_have_text("Skipped 4 already imported")
        expect(self.result_rows()).to_have_count(1)
        expect(self.result_rows().first).to_contain_text("Sample Bakery")
        expect(page.locator("#end-check")).to_have_text("✓ Ending balance matches YNAB: 1,200.00")

        # No duplicates in YNAB
        created = self.created(account)
        self.assertEqual(len(created), 5)
        self.assertEqual(len({t.import_id for t in created}), 5)
        self.assertEqual(self.ynab.balance(account), 1_200_000)

    def test_balance_gap_shows_and_flags_correction_rows(self):
        page = self.page
        account = fake_ynab.EXISTING_ACCOUNT_ID
        self.parse(self.gap_pdf)
        expect(page.locator("#parse-msg")).to_have_text(
            "Parsing successful: 4 GCash transactions from with_gap.pdf (+3 balance corrections)"
        )
        warning = page.locator("#balance-msg")
        expect(warning).to_be_visible()
        expect(warning.locator("strong")).to_have_text(
            "⚠ Balance does not add up — 3 correction rows added"
        )
        expect(warning.locator("dt")).to_have_text(
            [
                "2025-09-01 08:15 AM · STARTING BALANCE CORRECTION",
                "2025-09-03 06:05 PM · Refund from Sample Coffee Shop",
                "2025-09-04 09:30 AM · MISSING TRANSACTIONS IN GCASH STATEMENT",
            ]
        )
        expect(warning.locator("dd")).to_have_text(["+100.00", "+50.00", "-50.00"])
        expect(self.preview_rows()).to_have_count(7)
        missing = page.locator("#preview-table tbody tr.missing")
        expect(missing).to_have_count(3)
        expect(missing.first).to_contain_text("S9990000000011")

        self.connect()
        self.choose_account(account)
        expect(page.locator("#start-check")).to_have_text(
            "✓ Starting balance matches YNAB: 1,000.00"
        )
        expect(page.locator("#upload-btn")).to_have_text("Upload 7 transactions to YNAB")
        self.upload()
        expect(page.locator("#upload-msg")).to_contain_text("7 transactions created")
        expect(self.result_rows()).to_have_count(7)
        flagged = page.locator("#results-table tbody tr.missing")
        expect(flagged).to_have_count(3)
        expect(flagged.nth(0)).to_contain_text("STARTING BALANCE CORRECTION")
        expect(flagged.nth(1)).to_contain_text("Refund from Sample Coffee Shop")
        expect(flagged.nth(2)).to_contain_text("MISSING TRANSACTIONS IN GCASH STATEMENT")
        expect(page.locator("#end-check")).to_have_text("✓ Ending balance matches YNAB: 1,400.00")

        # Correction rows are red-flagged in YNAB, the statement's own rows aren't
        red = [t for t in self.created(account) if t.flag_color and t.flag_color.value == "red"]
        self.assertEqual(len(red), 3)
        self.assertTrue(all(re.fullmatch(r"GCash Ref: [SRM]\d+", t.memo) for t in red))
        self.assertEqual(self.ynab.balance(account), 1_400_000)

    def test_new_account_gets_the_statement_starting_balance(self):
        page = self.page
        account = fake_ynab.NEW_ACCOUNT_ID
        self.parse(self.balanced_pdf)
        self.connect()
        self.choose_account(account)
        expect(page.locator("#start-check")).to_have_text(
            "ⓘ New YNAB account: its Starting Balance (250.00) will be set to the "
            "statement starting balance (1,000.00) when you upload."
        )
        self.upload()
        expect(page.locator("#upload-msg")).to_contain_text("4 transactions created")
        expect(page.locator("#start-check")).to_have_text(
            "ⓘ New YNAB account: Starting Balance set from 250.00 to 1,000.00 "
            "(the statement starting balance)."
        )
        expect(page.locator("#end-check")).to_have_text("✓ Ending balance matches YNAB: 1,250.00")
        expect(page.locator("#upload-steps li")).to_have_text(
            [
                "Statement read: 4 rows",
                "Starting balance checked",
                "Uploaded 4 transactions to YNAB",
                "Ending balance checked",
            ]
        )
        expect(page.locator("#upload-steps li.done")).to_have_count(4)
        expect(page.locator("#upload-elapsed")).to_contain_text("Finished in")

        starting = [
            t
            for t in self.ynab.transactions(account)
            if t.payee_name == fake_ynab.STARTING_BALANCE_PAYEE
        ]
        self.assertEqual([t.amount for t in starting], [1_000_000])
        self.assertEqual(self.ynab.balance(account), 1_250_000)
        # The other account is untouched
        self.assertEqual(self.ynab.balance(fake_ynab.EXISTING_ACCOUNT_ID), 1_000_000)

    def test_wrong_token_stays_disconnected(self):
        page = self.page
        self.connect("wrong-token")
        expect(page.locator("#ynab-msg")).to_have_text(
            "YNAB rejected this token. Copy it again from YNAB Developer Settings."
        )
        expect(page.locator("#status-pill")).to_have_text("○ Not connected")
        expect(page.locator("#ynab-off")).to_be_visible()
        expect(page.locator("#ynab-on")).to_be_hidden()
        expect(page.locator("#card-target")).to_have_class(re.compile(r"\bdisabled\b"))
        expect(page.locator("#plan")).to_have_value("")
        self.assertNotIn("get_plans", [method for method, _ in self.ynab.calls])

    def test_not_a_pdf_shows_an_error(self):
        page = self.page
        page.set_input_files(
            "#pdf",
            [
                {
                    "name": "notes.pdf",
                    "mimeType": "application/pdf",
                    "buffer": b"This is a text file, not a PDF.",
                }
            ],
        )
        page.click("#parse-btn")
        expect(page.locator("#parse-msg")).to_contain_text("Could not read the PDF")
        expect(page.locator("#parse-msg")).to_have_class(re.compile(r"\berr\b"))
        # The step it stopped at turns red; the ones after it never ran
        expect(page.locator("#parse-steps li.failed")).to_have_text("Opening the PDF")
        expect(page.locator("#parse-steps li.todo")).to_have_count(1)
        expect(page.locator("#preview")).to_be_hidden()
        expect(page.locator("#card-parse")).not_to_have_class(re.compile(r"\bcomplete\b"))

    def test_disconnect_goes_back_to_token_entry(self):
        page = self.page
        self.connect()
        expect(page.locator("#status-pill")).to_have_text("● Connected")
        page.click("#disconnect-btn")
        expect(page.locator("#status-pill")).to_have_text("○ Not connected")
        expect(page.locator("#ynab-off")).to_be_visible()
        expect(page.locator("#ynab-on")).to_be_hidden()
        expect(page.locator("#plan option")).to_have_text(["— connect to YNAB first —"])

        # Still disconnected after a reload: the server forgot the token
        page.reload(wait_until="networkidle")
        expect(page.locator("#status-pill")).to_have_text("○ Not connected")
        expect(page.locator("#ynab-off")).to_be_visible()

    def test_reload_restores_the_preview_and_connection(self):
        page = self.page
        self.parse(self.balanced_pdf)
        self.connect()
        expect(page.locator("#status-pill")).to_have_text("● Connected")

        page.reload(wait_until="networkidle")
        expect(page.locator("#parse-msg")).to_have_text(
            "Parsing successful: 4 GCash transactions from balanced.pdf"
        )
        expect(self.preview_rows()).to_have_count(4)
        expect(page.locator("#preview-totals")).to_contain_text("matches statement ✓")
        expect(page.locator("#status-pill")).to_have_text("● Connected")
        # Plans are loaded again, so the upload can carry on
        self.choose_account(fake_ynab.EXISTING_ACCOUNT_ID)
        expect(page.locator("#upload-btn")).to_have_text("Upload 4 transactions to YNAB")


class HostedWebFlowTest(BrowserTest):
    """Hosted mode (PUBLIC_URL set): YNAB login is the only way to connect."""

    hosted = True

    def test_only_the_ynab_login_shows(self):
        page = self.page
        expect(page.locator("#connect-btn")).to_be_visible()
        expect(page.locator("#connect-btn")).to_have_text("Connect with YNAB login")
        for token_part in ("#ynab-token", "#token-btn", "#token-note", "#remember-row"):
            expect(page.locator(token_part)).to_have_count(0)
        expect(page.locator("#status-pill")).to_have_text("○ Not connected")
        self.assertEqual(self.console_errors, [])

    def test_connect_goes_to_the_ynab_login_page(self):
        page = self.page
        redirects = []

        def login(route):
            # The app's redirect to YNAB, not followed: nothing reaches the real YNAB
            # (page.route() doesn't see requests that come from a redirect)
            response = route.fetch(max_redirects=0)
            redirects.append(response.headers["location"])
            route.fulfill(body="Fake YNAB login page")

        page.route("**/oauth/login", login)
        page.click("#connect-btn")
        expect(page.locator("body")).to_have_text("Fake YNAB login page")
        self.assertEqual(len(redirects), 1)
        self.assertTrue(redirects[0].startswith("https://app.ynab.com/oauth/authorize?"))
        query = urllib.parse.parse_qs(urllib.parse.urlparse(redirects[0]).query)
        self.assertEqual(query["redirect_uri"], [f"{PUBLIC_URL}/oauth/callback"])
        self.assertEqual(query["response_type"], ["code"])


if __name__ == "__main__":
    unittest.main()
