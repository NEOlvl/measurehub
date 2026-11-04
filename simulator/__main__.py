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
    return parser


def _env_int(name: str) -> int | None:
    value = os.environ.get(name)
    return int(value) if value else None


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    eps_re, eps_loss = args.eps
    mu_re, mu_loss = args.mu
    sample = Sample.from_parts(eps_re, eps_loss, mu_re, mu_loss, args.thickness_mm / 1000)
    engine = ScpiEngine(sample, seed=args.seed, noise_k=args.noise_k, time_scale=args.time_scale)
    server = SimulatorServer(engine, args.host, args.port)

    async def run() -> None:
        port = await server.start()
        print(f"SIM-VNA listening on {args.host}:{port}", flush=True)
        await server.serve_forever()

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
