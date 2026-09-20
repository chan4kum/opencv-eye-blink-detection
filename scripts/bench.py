"""Minimal closed-loop load generator for /v1/eye-state (no extra dependencies beyond httpx).

Example:  uv run python scripts/bench.py --url http://127.0.0.1:8000 --image tests/data/astronaut.jpg -c 8 -d 20
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from pathlib import Path

import httpx


async def worker(
    client: httpx.AsyncClient,
    url: str,
    body: bytes,
    headers: dict[str, str],
    stop: float,
    lat: list[float],
    codes: dict[int, int],
) -> None:
    while time.perf_counter() < stop:
        start = time.perf_counter()
        try:
            r = await client.post(url, content=body, headers=headers)
            code = r.status_code
        except httpx.HTTPError:
            code = 0
        lat.append(time.perf_counter() - start)
        codes[code] = codes.get(code, 0) + 1


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--image", type=Path, required=True)
    p.add_argument("-c", "--concurrency", type=int, default=8)
    p.add_argument("-d", "--duration", type=float, default=15.0)
    p.add_argument("--warmup", type=float, default=2.0)
    p.add_argument("--api-key", default=None)
    args = p.parse_args()

    body = args.image.read_bytes()
    headers = {"content-type": "image/jpeg"}
    if args.api_key:
        headers["authorization"] = f"Bearer {args.api_key}"
    url = f"{args.url}/v1/eye-state"
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    async with httpx.AsyncClient(limits=limits, timeout=30) as client:
        await asyncio.gather(
            *(
                worker(client, url, body, headers, time.perf_counter() + args.warmup, [], {})
                for _ in range(args.concurrency)
            )
        )
        lat: list[float] = []
        codes: dict[int, int] = {}
        t0 = time.perf_counter()
        stop = t0 + args.duration
        await asyncio.gather(*(worker(client, url, body, headers, stop, lat, codes) for _ in range(args.concurrency)))
        elapsed = time.perf_counter() - t0

    lat_ms = sorted(x * 1000 for x in lat)

    def pct(q: float) -> float:
        return lat_ms[min(len(lat_ms) - 1, int(q * len(lat_ms)))]

    print(
        f"concurrency={args.concurrency} duration={elapsed:.1f}s requests={len(lat)} status={dict(sorted(codes.items()))}"
    )
    print(
        f"throughput={len(lat) / elapsed:.1f} req/s  mean={statistics.fmean(lat_ms):.1f}ms "
        f"p50={pct(0.5):.1f}ms p95={pct(0.95):.1f}ms p99={pct(0.99):.1f}ms max={lat_ms[-1]:.1f}ms"
    )


if __name__ == "__main__":
    asyncio.run(main())
