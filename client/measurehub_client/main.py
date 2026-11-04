"""Entry point: ``measurehub-client [--url http://localhost:8000]`` (MH-55)."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from PyQt6.QtWidgets import QApplication, QDialog

from measurehub_client.api import ApiClient
from measurehub_client.controller import Controller
from measurehub_client.login import LoginDialog
from measurehub_client.main_window import MainWindow

DEFAULT_URL = "http://localhost:8000"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="measurehub-client", description="MeasureHub operator client"
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("MH_URL", DEFAULT_URL),
        help=f"server address (env MH_URL, default {DEFAULT_URL})",
    )
    parser.add_argument("--username", default="", help="prefill the login field")
    args = parser.parse_args(argv)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("MeasureHub")
    controller = Controller(ApiClient(args.url))
    login = LoginDialog(controller, args.username)
    if login.exec() != QDialog.DialogCode.Accepted:
        return 0
    window = MainWindow(controller, login.username.text().strip())
    window.show()
    window.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
