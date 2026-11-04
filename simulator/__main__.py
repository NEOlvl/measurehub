"""``python -m simulator`` / ``measurehub-sim``: run the SCPI simulator on a TCP port."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
from collections.abc import Sequence

from simulator.model import DEMO_SAMPLE, Sample
from simulator.scpi import ScpiEngine
from simulator.server import SimulatorServer


def _pair(text: str) -> tuple[float, float]:
    first, second = text.split(",")
    return float(first), float(second)


def build_parser() -> argparse.ArgumentParser:
    env = os.environ.get
    demo = DEMO_SAMPLE
    parser = argparse.ArgumentParser(prog="measurehub-sim", description=__doc__)
    parser.add_argument("--host", default=env("MH_SIM_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(env("MH_SIM_PORT", "5025")))
    parser.add_argument(
        "--eps",
        type=_pair,
        default=_pair(env("MH_SIM_EPS", f"{demo.eps.real},{-demo.eps.imag}")),
        metavar="RE,LOSS",
        help="sample permittivity (eps', eps'')",
    )
    parser.add_argument(
        "--mu",
        type=_pair,
        default=_pair(env("MH_SIM_MU", f"{demo.mu.real},{-demo.mu.imag}")),
        metavar="RE,LOSS",
        help="sample permeability (mu', mu'')",
    )
    parser.add_argument(
        "--thickness-mm",
        type=float,
        default=float(env("MH_SIM_THICKNESS_MM", str(demo.thickness_m * 1000))),
    )
    parser.add_argument("--seed", type=int, default=_env_int("MH_SIM_SEED"))
    parser.add_argument("--noise-k", type=float, default=float(env("MH_SIM_NOISE_K", "1e-3")))
    parser.add_argument(
        "--time-scale",
        type=float,
        default=float(env("MH_SIM_TIME_SCALE", "1")),
        help="multiplier of the simulated sweep time (0 = instant)",
    )
    parser.add_argument(
        "--fault-drop",
        type=float,
        default=float(env("MH_SIM_FAULT_DROP", "0")),
        help="probability that a received line closes the connection",
    )
    parser.add_argument(
        "--fault-hang",
        type=float,
        default=float(env("MH_SIM_FAULT_HANG", "0")),
        help="probability that a received line is never answered",
    )
    parser.add_argument(
        "--fault-err",
        type=float,
        default=float(env("MH_SIM_FAULT_ERR", "0")),
        help="probability that a received line fails with an instrument error",
    )
    parser.add_argument(
        "--fault-latency-ms",
        type=float,
        default=float(env("MH_SIM_FAULT_LATENCY_MS", "0")),
        help="extra delay of every answer",
    )
    parser.add_argument("--fault-seed", type=int, default=_env_int("MH_SIM_FAULT_SEED"))
    return parser


def _env_int(name: str) -> int | None:
    value = os.environ.get(name)
    return int(value) if value else None


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    eps_re, eps_loss = args.eps
    mu_re, mu_loss = args.mu
    sample = Sample.from_parts(eps_re, eps_loss, mu_re, mu_loss, args.thickness_mm / 1000)
    engine = ScpiEngine(
        sample,
        seed=args.seed,
        noise_k=args.noise_k,
        time_scale=args.time_scale,
        fault_drop=args.fault_drop,
        fault_hang=args.fault_hang,
        fault_err=args.fault_err,
        fault_latency_ms=args.fault_latency_ms,
        fault_seed=args.fault_seed,
    )
    server = SimulatorServer(engine, args.host, args.port)

    async def run() -> None:
        port = await server.start()
        print(f"SIM-VNA listening on {args.host}:{port}", flush=True)
        await server.serve_forever()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
