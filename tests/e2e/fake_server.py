"""
The web app with the fake YNAB (fake_ynab.py), served on 127.0.0.1 from a
background thread, for the browser tests.

    with FakeServer() as server:     # free port
        server.url                   # "http://127.0.0.1:<port>"
        server.ynab.reset()          # fresh YNAB state, e.g. per test

Run it by hand to reproduce a CI failure in your own browser:

    python -m tests.e2e.fake_server  # http://127.0.0.1:5099, token "test-token"

It also writes the made-up statement PDFs (make_pdf.py) to a temp folder and
prints their paths. Nothing reaches the real YNAB or the OS secure store.
"""

import logging
import sys
import tempfile
import threading
from pathlib import Path

from werkzeug.serving import make_server

import tests  # noqa: F401 - the safety net: nothing reaches YNAB or the keyring
from statement_etl.web import app as web_app
from tests.e2e import make_pdf
from tests.e2e.fake_ynab import TOKEN, FakeYnab

MANUAL_PORT = 5099


class FakeServer:
    def __init__(self, port=0, quiet=True):
        self.port = port
        self.quiet = quiet  # hide werkzeug's line per request
        self.ynab = FakeYnab()
        self._patches = None
        self._server = None
        self._thread = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def reset(self):
        """Fresh YNAB state and no browser sessions."""
        self.ynab.reset()
        web_app.STORE.clear()

    def start(self):
        if self.quiet:
            logging.getLogger("werkzeug").setLevel(logging.ERROR)
        self._patches = self.ynab.patch()
        self._patches.__enter__()
        try:
            self._server = make_server("127.0.0.1", self.port, web_app.app)
        except BaseException:
            self._patches.close()
            raise
        self.port = self._server.server_port
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)
        if self._patches:
            self._patches.close()
        web_app.STORE.clear()
        self._server = self._thread = self._patches = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


def main():
    pdf_dir = Path(tempfile.mkdtemp(prefix="statement-etl-e2e-"))
    make_pdf.balanced(pdf_dir / "balanced.pdf")
    make_pdf.with_gap(pdf_dir / "with_gap.pdf")
    server = FakeServer(MANUAL_PORT, quiet=False).start()
    print(f"Fake YNAB app on {server.url}", file=sys.stderr)
    print(f"YNAB token: {TOKEN}   (any PDF password works)", file=sys.stderr)
    print(f"Statement PDFs: {pdf_dir}", file=sys.stderr)
    print("Ctrl+C to stop.", file=sys.stderr)
    try:
        server._thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()
