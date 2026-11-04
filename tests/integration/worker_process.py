"""Subprocess entry point: run one job the way a Celery worker would.

Used by the crash test, which kills this process in the middle of a measurement.
Usage: worker_process.py DB_URL REDIS_PORT JOB_ID LOCK_TTL_S
"""

from __future__ import annotations

import sys
import uuid

import redis
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from measurehub.drivers.registry import create_driver
from measurehub.worker.engine import EngineContext, execute_job


def main(db_url: str, redis_port: str, job_id: str, lock_ttl_s: str) -> None:
    engine = create_engine(db_url)
    ctx = EngineContext(
        session_factory=sessionmaker(engine, expire_on_commit=False),
        redis=redis.Redis(port=int(redis_port)),
        driver_factory=lambda key, resource: create_driver(
            key, resource, timeout_ms=3000, connect_attempts=1
        ),
        lock_ttl_s=int(lock_ttl_s),
    )
    print(execute_job(uuid.UUID(job_id), ctx), flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:5])
