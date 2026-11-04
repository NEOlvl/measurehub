import json
import logging

import pytest
import structlog

from measurehub.core.logging import configure_logging


def test_events_and_foreign_records_are_json_with_context(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO")
    with structlog.contextvars.bound_contextvars(job_id="j-1", instrument_id="i-1"):
        structlog.get_logger().info("segment_saved", segment=3)
        logging.getLogger("celery.app.trace").info("Task succeeded")
    structlog.get_logger().debug("hidden")

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["event"] for line in lines] == ["segment_saved", "Task succeeded"]
    assert lines[0]["segment"] == 3
    for line in lines:
        assert line["job_id"] == "j-1"
        assert line["instrument_id"] == "i-1"
        assert line["level"] == "info"
        assert "timestamp" in line
