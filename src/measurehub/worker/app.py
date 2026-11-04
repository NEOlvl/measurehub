"""Celery application (reliability settings from the technical specification, 5.2)."""

from __future__ import annotations

from celery import Celery
from celery.signals import setup_logging

from measurehub.core.logging import configure_logging
from measurehub.core.settings import Settings, get_settings


def make_celery(settings: Settings) -> Celery:
    app = Celery("measurehub", broker=settings.redis_url, include=["measurehub.worker.tasks"])
    app.conf.update(
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        task_reject_on_worker_lost=True,
        task_ignore_result=True,
        broker_connection_retry_on_startup=True,
        broker_transport_options={"visibility_timeout": 3600},
        worker_hijack_root_logger=False,
        worker_redirect_stdouts=False,
        beat_schedule={
            "watchdog": {
                "task": "measurehub.watchdog",
                "schedule": float(settings.watchdog_interval_s),
            },
            "poll-instruments": {
                "task": "measurehub.poll_instruments",
                "schedule": float(settings.poll_interval_s),
            },
        },
    )
    return app


@setup_logging.connect
def _configure_worker_logging(**_: object) -> None:
    """Replace Celery's own logging setup with the JSON configuration."""
    configure_logging(get_settings().log_level)


celery_app = make_celery(get_settings())
