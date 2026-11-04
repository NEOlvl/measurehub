"""Command line interface: ``measurehub measure``."""

from __future__ import annotations

import argparse
import csv
import sys
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path

from pydantic import ValidationError

from measurehub.core.sample import SampleSpec, parse_sample
from measurehub.drivers.errors import DriverError
from measurehub.drivers.models import SegmentData, VnaSweepParams
from measurehub.drivers.sim_vna import SimVnaDriver
from measurehub.drivers.sweep import run_sweep
from measurehub.drivers.transport import VisaTransport
from measurehub.processing import registry
from measurehub.processing.base import ProcessorInput, ProcessorResult

_TRUTH_KEYS = {
    "eps_re_mean": ("eps", 0),
    "eps_im_mean": ("eps", 1),
    "mu_re_mean": ("mu", 0),
    "mu_im_mean": ("mu", 1),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="measurehub")
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("measure", help="sweep a sample and compute its eps and mu")
    m.add_argument(
        "--sample",
        help="sample as 'd_mm=3;eps=10,0.5;mu=1.5,0.2' (thickness; true eps', eps'' and "
        "mu', mu'' for the simulator). Default: built-in demo sample",
    )
    m.add_argument(
        "--resource",
        help="VISA resource, e.g. TCPIP0::localhost::5025::SOCKET. "
        "Default: start an embedded simulator",
    )
    m.add_argument("--out", type=Path, required=True, help="result CSV file")
    m.add_argument("--processor", default="nrw", choices=registry.available())
    m.add_argument("--start", type=float, default=1e9, help="start frequency, Hz")
    m.add_argument("--stop", type=float, default=10e9, help="stop frequency, Hz")
    m.add_argument("--points", type=int, default=901)
    m.add_argument("--ifbw", type=float, default=1000.0, help="IF bandwidth, Hz")
    m.add_argument("--averaging", type=int, default=1)
    m.add_argument("--segment-size", type=int, default=50)
    m.add_argument("--seed", type=int, help="noise seed of the embedded simulator")
    m.add_argument(
        "--sim-time-scale",
        type=float,
        default=0.0,
        help="sweep-time multiplier of the embedded simulator (0 = instant, 1 = real time)",
    )
    m.add_argument("--timeout-ms", type=int, default=5000)

    u = sub.add_parser("create-user", help="create an API user (password from --password or env)")
    u.add_argument("--username", help="default: MH_BOOTSTRAP_USERNAME")
    u.add_argument("--password", help="default: MH_BOOTSTRAP_PASSWORD")
    u.add_argument(
        "--if-missing", action="store_true", help="do nothing if the user already exists"
    )
    return parser


def write_csv(path: Path, result: ProcessorResult) -> None:
    flag_names = list(result.flags)
    header = ["freq_hz", *result.columns, *flag_names]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for i, freq in enumerate(result.freq_hz):
            row = [f"{freq:.9g}"]
            row += [f"{col[i]:.9g}" for col in result.columns.values()]
            row += [str(int(result.flags[name][i])) for name in flag_names]
            writer.writerow(row)


def _report(result: ProcessorResult, spec: SampleSpec) -> None:
    for key, value in result.summary.items():
        line = f"{key}: {value:.6g}"
        truth_ref = _TRUTH_KEYS.get(key)
        if truth_ref is not None:
            truth = getattr(spec, truth_ref[0])
            if truth is not None:
                expected = truth[truth_ref[1]]
                line += f"  (true {expected:g}, error {abs(value - expected) / expected:.2%})"
        print(line)


def _progress(data: SegmentData, total: int) -> None:
    if sys.stderr.isatty():
        print(f"\rsegment {data.segment.index + 1}/{total}", end="", file=sys.stderr, flush=True)
        if data.segment.index + 1 == total:
            print(file=sys.stderr)


def run_measure(args: argparse.Namespace) -> int:
    spec = parse_sample(args.sample) if args.sample else SampleSpec()
    processor = registry.get_processor(args.processor)
    params = VnaSweepParams(
        start_hz=args.start,
        stop_hz=args.stop,
        points=args.points,
        ifbw_hz=args.ifbw,
        averaging=args.averaging,
        segment_size=args.segment_size,
    )
    with ExitStack() as stack:
        resource = args.resource
        if resource is None:
            # Imported lazily: the simulator is only needed for the embedded mode.
            from simulator.model import DEMO_SAMPLE, Sample
            from simulator.scpi import ScpiEngine
            from simulator.server import SimulatorThread

            eps = spec.eps or (DEMO_SAMPLE.eps.real, -DEMO_SAMPLE.eps.imag)
            mu = spec.mu or (DEMO_SAMPLE.mu.real, -DEMO_SAMPLE.mu.imag)
            if spec.thickness_m is None:
                spec = SampleSpec(DEMO_SAMPLE.thickness_m, eps, mu)
            else:
                spec = SampleSpec(spec.thickness_m, eps, mu)
            engine = ScpiEngine(
                Sample.from_parts(*eps, *mu, spec.thickness_m or 0.0),
                seed=args.seed,
                time_scale=args.sim_time_scale,
            )
            sim = stack.enter_context(SimulatorThread(engine))
            resource = f"TCPIP0::127.0.0.1::{sim.port}::SOCKET"
        if processor.name == "nrw" and spec.thickness_m is None:
            print("error: nrw needs the sample thickness (--sample d_mm=...)", file=sys.stderr)
            return 2

        driver = SimVnaDriver(VisaTransport(resource, timeout_ms=args.timeout_ms))
        driver.connect()
        stack.callback(driver.close)
        info = driver.identify()
        print(f"instrument: {info.raw}")
        sweep = run_sweep(driver, params, on_segment=_progress)

    result = processor.process(
        ProcessorInput(sweep.freq_hz, sweep.s11, sweep.s21, spec.thickness_m)
    )
    write_csv(args.out, result)
    print(f"{params.points} points -> {args.out}")
    _report(result, spec)
    return 0


def run_create_user(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from measurehub.core.security import hash_password
    from measurehub.core.settings import get_settings
    from measurehub.db.models import User
    from measurehub.db.session import make_sync_engine, make_sync_sessionmaker

    settings = get_settings()
    username = args.username or settings.bootstrap_username
    password = args.password or settings.bootstrap_password
    if not username or not password:
        print("error: username and password are required", file=sys.stderr)
        return 2
    engine = make_sync_engine(settings)
    with make_sync_sessionmaker(engine)() as session:
        if session.scalar(select(User).where(User.username == username)) is not None:
            print(f"user {username!r} already exists")
            return 0 if args.if_missing else 1
        session.add(User(username=username, password_hash=hash_password(password)))
        session.commit()
    print(f"user {username!r} created")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create-user":
            return run_create_user(args)
        return run_measure(args)
    except (DriverError, ValidationError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
