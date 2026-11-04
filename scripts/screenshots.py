"""Take screenshots of the client against a running stack (docs/screenshots/*.png).

    docker compose up -d --build
    python scripts/screenshots.py [--url http://localhost:8000]

The script creates an instrument, a method and a sample through the API (names get a random
suffix) and then drives the real client windows. It needs a display: QT_QPA_PLATFORM=offscreen
has no fonts, so the text would render as boxes (use xvfb on a headless Linux machine).
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

import httpx
from PyQt6.QtWidgets import QApplication

from measurehub_client.api import ApiClient
from measurehub_client.controller import Controller
from measurehub_client.main_window import MainWindow

OUT = Path(__file__).resolve().parents[1] / "docs" / "screenshots"


def pump(app: QApplication, seconds: float, until=None) -> None:  # type: ignore[no-untyped-def]
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if until is not None and until():
            return
        time.sleep(0.02)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--username", default="operator")
    parser.add_argument("--password", default="operator-dev-password")
    parser.add_argument("--simulator", default="TCPIP0::simulator::5025::SOCKET")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    suffix = uuid.uuid4().hex[:6]
    api = ApiClient(args.url)
    api.login(args.username, args.password)
    headers = {"Authorization": f"Bearer {api.token}"}
    with httpx.Client(base_url=f"{args.url}/api/v1", headers=headers) as http:
        instrument = http.post(
            "/instruments",
            json={"name": f"VNA-{suffix}", "resource": args.simulator, "driver": "sim_vna"},
        ).json()
        method = http.post(
            "/methods",
            json={
                "name": f"NRW-{suffix}",
                "params": {"points": 601, "segment_size": 50, "ifbw_hz": 300},
            },
        ).json()
        sample = http.post(
            "/samples",
            json={"code": f"F-{suffix}", "material": "феррит", "thickness_mm": 3.0},
        ).json()
        http.post(f"/instruments/{instrument['id']}/check")

    app = QApplication(sys.argv[:1])
    controller = Controller(api)
    window = MainWindow(controller, args.username)
    window.resize(1280, 800)
    window.show()
    window.start()
    pump(app, 3)
    window.tabs.setCurrentWidget(window.instruments)
    pump(app, 0.5)
    window.grab().save(str(OUT / "instruments.png"))

    job = api.create_job(method["id"], instrument["id"], sample["id"])
    card = window.open_job(job["id"])
    pump(app, 60, until=lambda: card.curve_points() >= 250)
    window.grab().save(str(OUT / "job-live.png"))
    pump(app, 120, until=lambda: card.status == "completed" and card.curve_points() == 601)
    pump(app, 1)
    window.grab().save(str(OUT / "job-completed.png"))

    window.tabs.setCurrentWidget(window.jobs)
    pump(app, 1)
    window.grab().save(str(OUT / "jobs.png"))

    result = window.open_result(job["id"])
    pump(app, 30, until=lambda: result.result is not None)
    pump(app, 0.5)
    window.grab().save(str(OUT / "result.png"))
    window.close()
    print(f"saved {sorted(p.name for p in OUT.glob('*.png'))} to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
