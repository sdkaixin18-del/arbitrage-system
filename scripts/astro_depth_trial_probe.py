"""Bounded read-only SSH/WireGuard comparison. No production/card imports."""
import argparse
import asyncio
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import statistics
import subprocess
import time
import uuid

import httpx

ROUTES = [
    ("binance", "https://api.binance.com/api/v3/depth", {"symbol": "BTCUSDT", "limit": 5}),
    ("bitget", "https://api.bitget.com/api/v2/spot/market/orderbook", {"symbol": "BTCUSDT", "limit": 5, "type": "step0"}),
    ("aster", "https://fapi.asterdex.com/fapi/v1/depth", {"symbol": "BTCUSDT", "limit": 5}),
]


def validate(envelope, body, now_ms):
    if (envelope.get("service") != "astro-depth-cloud" or envelope.get("protocol") != 1
            or envelope.get("requestId") != body["requestId"] or envelope.get("url") != body["url"]):
        raise ValueError("identity_mismatch")
    if envelope.get("statusCode") != 200:
        raise ValueError("exchange_status_" + str(envelope.get("statusCode")))
    age = now_ms - int(envelope["receivedAtMs"])
    if not -1000 <= age <= 3000:
        raise ValueError("stale_or_clock_skew")
    if int(envelope["requestStartedAtMs"]) > int(envelope["receivedAtMs"]):
        raise ValueError("invalid_timestamps")
    payload = envelope["payload"]
    if "bitget" in body["url"]:
        if payload.get("code") != "00000":
            raise ValueError("exchange_business_error")
        book = payload["data"]
    else:
        book = payload
    sides = []
    for side in ("bids", "asks"):
        levels = [(float(level[0]), float(level[1])) for level in book[side]]
        if not levels or any(not math.isfinite(p) or not math.isfinite(q) or p <= 0 or q <= 0 for p, q in levels):
            raise ValueError("invalid_depth")
        sides.append(levels)
    if sides[0][0][0] > sides[1][0][0]:
        raise ValueError("crossed_depth")
    exchange_ts = book.get("ts") or book.get("E")
    exchange_age = now_ms - int(exchange_ts) if exchange_ts else None
    if exchange_age is not None and not -1000 <= exchange_age <= 3000:
        raise ValueError("exchange_depth_stale")
    return {"ageMs": age, "exchangeAgeMs": exchange_age,
            "queueWaitMs": envelope["queueWaitMs"],
            "upstreamMs": envelope["receivedAtMs"] - envelope["requestStartedAtMs"],
            "payloadHash": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()}


async def fetch(client, path, body):
    started = time.perf_counter()
    try:
        response = await client.post("/v1/public-get", json=body)
        response.raise_for_status()
        detail = validate(response.json(), body, int(time.time() * 1000))
        return {"path": path, "ok": True, "ms": (time.perf_counter() - started) * 1000, **detail}
    except Exception as exc:
        error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        if isinstance(exc, httpx.HTTPStatusError):
            error += "_" + str(exc.response.status_code)
        return {"path": path, "ok": False, "ms": (time.perf_counter() - started) * 1000, "error": error}


async def race(clients, body, delay=0.2, primary="wg"):
    started = time.perf_counter()
    secondary = "ssh" if primary == "wg" else "wg"
    first = asyncio.create_task(fetch(clients[primary], primary, body))
    await asyncio.wait({first}, timeout=delay)
    if first.done() and first.result()["ok"]:
        return first.result(), [first.result()]
    second = asyncio.create_task(fetch(clients[secondary], secondary, body))
    tasks = [first, second]
    winner = None
    try:
        for task in asyncio.as_completed(tasks):
            result = await task
            if result["ok"]:
                winner = {**result, "ms": (time.perf_counter() - started) * 1000}
                break
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    results = [{"path": path, "cancelled": True} if task.cancelled() else task.result()
               for path, task in zip((primary, secondary), tasks)]
    return winner or {"ok": False, "ms": (time.perf_counter() - started) * 1000,
                      "error": "both_paths_failed"}, results


def summarize(rows):
    summary = {}
    for mode in ("ssh", "dual"):
        subset = [row for row in rows if row["mode"] == mode]
        good = [row["result"] for row in subset if row["result"]["ok"]]
        delays = sorted(row["ms"] for row in good)
        summary[mode] = {"total": len(subset), "valid": len(good),
                         "p50Ms": round(statistics.median(delays), 1) if delays else None,
                         "p95Ms": round(delays[math.ceil(len(delays) * .95) - 1], 1) if delays else None,
                         "maxQueueMs": max((r["queueWaitMs"] for r in good), default=None),
                         "errors": dict(Counter(r["result"].get("error") for r in subset if not r["result"]["ok"]))}
    return summary


async def run(args, token, bridge):
    headers = {"Authorization": "Bearer " + token}
    async with httpx.AsyncClient(base_url="http://127.0.0.1:18867", headers=headers, trust_env=False, timeout=8) as wg, \
            httpx.AsyncClient(base_url="http://127.0.0.1:18868", headers=headers, trust_env=False, timeout=8) as ssh:
        clients = {"wg": wg, "ssh": ssh}
        report = {"startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "health": {}, "rows": []}
        for name, client in clients.items():
            try:
                before = time.time() * 1000
                response = await client.get("/health")
                response.raise_for_status()
                health = response.json()
                after = time.time() * 1000
                health["rttMs"] = round(after - before, 1)
                health["clockOffsetEstimateMs"] = round(health["serverTimeMs"] - (before + after)/2, 1)
                report["health"][name] = health
            except Exception as exc:
                report["health"][name] = {"error": type(exc).__name__}
        print(json.dumps({"health": report["health"]}), flush=True)
        report["mode"] = "ssh-only" if args.ssh_only else "comparison"
        report["primary"] = args.primary
        required = ("ssh",) if args.ssh_only else ("ssh", "wg")
        if any("error" in report["health"][name] for name in required):
            args.output.write_text(json.dumps(report, indent=2))
            raise RuntimeError("Both paths must pass health before comparison")
        # Force both paths once to verify real-cloud dedup, independent of hedge timing.
        venue, url, params = ROUTES[0]
        body = {"url": url, "params": params, "requestId": uuid.uuid4().hex}
        check_clients = {"ssh_a": ssh, "ssh_b": ssh} if args.ssh_only else clients
        report["dedupTransports"] = list(check_clients)
        check = await asyncio.gather(*(fetch(client, name, body) for name, client in check_clients.items()))
        report["dedupCheck"] = check
        if not all(r["ok"] for r in check) or check[0]["payloadHash"] != check[1]["payloadHash"]:
            args.output.write_text(json.dumps(report, indent=2))
            raise RuntimeError("Real-cloud duplicate payload comparison failed")
        for iteration in range(args.rounds):
            for venue, url, params in ROUTES:
                modes = ("ssh",) if args.ssh_only else (("ssh", "dual") if iteration % 2 == 0 else ("dual", "ssh"))
                for mode in modes:
                    body = {"url": url, "params": params, "requestId": uuid.uuid4().hex}
                    if mode == "dual":
                        winner, paths = await race(clients, body, primary=args.primary)
                    else:
                        winner = await fetch(ssh, "ssh", body)
                        paths = [winner]
                    report["rows"].append({"round": iteration, "venue": venue, "mode": mode,
                                           "result": winner, "paths": paths})
                    await asyncio.sleep(args.interval)
            print(json.dumps({"round": iteration+1, "summary": summarize(report["rows"])}), flush=True)
        if args.fault_check:
            bridge.terminate()
            await asyncio.to_thread(bridge.wait, timeout=4)
            venue, url, params = ROUTES[0]
            body = {"url": url, "params": params, "requestId": uuid.uuid4().hex}
            winner, paths = await race(clients, body, primary="wg")
            report["wireguardDisconnect"] = {"result": winner, "paths": paths}
            if not winner["ok"] or winner.get("path") != "ssh":
                args.output.write_text(json.dumps(report, indent=2))
                raise RuntimeError("SSH failed to take over after WireGuard disconnect")
        report["finalHealth"] = (await ssh.get("/health")).json()
        report["summary"] = summarize(report["rows"])
        args.output.write_text(json.dumps(report, indent=2))
        print(json.dumps({"complete": True, "summary": report["summary"],
                          "metrics": report["finalHealth"].get("metrics")}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--bridge", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=8)
    parser.add_argument("--interval", type=float, default=3)
    parser.add_argument("--ssh-only", action="store_true", help="Baseline only, not a dual-path comparison")
    parser.add_argument("--primary", choices=("ssh", "wg"), default="wg")
    parser.add_argument("--fault-check", action="store_true", help="Disconnect only the owned trial WG bridge at the end")
    args = parser.parse_args()
    if not 1 <= args.rounds <= 20 or args.interval < 2:
        parser.error("bounded trial: 1-20 rounds, interval >= 2 seconds")
    config = json.loads(args.config.read_text())
    bridge = subprocess.Popen([args.bridge, "--config", str(args.config), "--endpoint", args.endpoint])
    try:
        time.sleep(1)
        if bridge.poll() is not None:
            raise RuntimeError("Bridge failed to start")
        asyncio.run(run(args, config["Token"], bridge))
    finally:
        bridge.terminate()
        try:
            bridge.wait(timeout=4)
        except subprocess.TimeoutExpired:
            bridge.kill()
            bridge.wait()


if __name__ == "__main__":
    main()
