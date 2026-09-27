"""Bounded fault injection against owned SSH only; cloud service stays running."""
import argparse
import json
import logging
from pathlib import Path
import sys
import time
import uuid

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.astro_depth_trial_transport import PersistentTrialTunnel
from astro_depth_trial_probe import ROUTES, validate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.config.stat().st_mode & 0o077:
        raise RuntimeError("Config must be owner-only")
    token = json.loads(args.config.read_text())["Token"]
    tunnel = PersistentTrialTunnel(args.host, args.identity)
    report = {"rounds": [], "readOnly": True}
    try:
        tunnel.start()
        with httpx.Client(base_url="http://127.0.0.1:18868", trust_env=False, timeout=3,
                          headers={"Authorization": "Bearer " + token}) as client:
            def wait_health():
                deadline = time.monotonic() + 35
                while time.monotonic() < deadline:
                    try:
                        response = client.get("/health")
                        response.raise_for_status()
                        if response.json().get("service") != "astro-depth-trial":
                            raise RuntimeError("Wrong trial service")
                        tunnel.mark_healthy()
                        return
                    except httpx.HTTPError:
                        time.sleep(.5)
                raise RuntimeError("Trial recovery exceeded 35 seconds")
            wait_health()
            report["before"] = tunnel.snapshot()
            for index in range(2):
                before = tunnel.snapshot()
                started = time.monotonic()
                tunnel.disconnect_for_test()
                # Observe backoff before checking health; a cached response cannot pass.
                deadline = started + 5
                while tunnel.snapshot()["attempts"] == before["attempts"] and tunnel.snapshot()["state"] != "backoff":
                    if time.monotonic() > deadline:
                        raise RuntimeError("Disconnect was not detected")
                    time.sleep(.05)
                backoff = tunnel.snapshot()
                wait_health()
                recovery_ms = round((time.monotonic() - started) * 1000, 1)
                after = tunnel.snapshot()
                if after["attempts"] != before["attempts"] + 1 or after["pid"] == before["pid"]:
                    raise RuntimeError("Unexpected trial connection ownership or reconnect count")
                venue, url, params = ROUTES[index]
                body = {"url": url, "params": params, "requestId": uuid.uuid4().hex}
                response = client.post("/v1/public-get", json=body)
                response.raise_for_status()
                quote = validate(response.json(), body, int(time.time() * 1000))
                report["rounds"].append({"index": index + 1, "venue": venue, "recoveryMs": recovery_ms,
                                         "backoff": backoff, "after": after, "quote": quote})
                print(json.dumps(report["rounds"][-1]), flush=True)
    finally:
        tunnel.stop()
        report["final"] = tunnel.snapshot()
        args.output.write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
