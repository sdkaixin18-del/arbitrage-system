#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request


ALLOWED_PATHS = {
    "/v1/transfer-watch",
    "/v1/transfer-watch/refresh",
    "/v1/transfer-watch/ack",
    "/health",
    "/v1/funding-formation",
    "/v1/funding-formation/batch",
    "/v1/funding-formation/watch",
    "/v1/funding-formation/review",
}


def main() -> int:
    try:
        envelope = json.load(sys.stdin)
        method = str(envelope.get("method") or "GET").upper()
        path = str(envelope.get("path") or "")
        if method not in {"GET", "POST", "PUT"} or path not in ALLOWED_PATHS:
            raise ValueError("unsupported funding cloud RPC request")
        query = envelope.get("query") if isinstance(envelope.get("query"), dict) else {}
        encoded_query = urllib.parse.urlencode(
            {key: value for key, value in query.items() if value is not None}
        )
        url = f"http://127.0.0.1:8766{path}"
        if encoded_query:
            url = f"{url}?{encoded_query}"
        payload = envelope.get("payload")
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                status_code = response.status
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status_code = exc.code
            raw = exc.read()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {"detail": raw.decode("utf-8", errors="replace")[:1000]}
        json.dump({"statusCode": status_code, "data": data}, sys.stdout, ensure_ascii=False)
        return 0
    except Exception as exc:  # noqa: BLE001
        json.dump({"statusCode": 503, "data": {"detail": str(exc)}}, sys.stdout, ensure_ascii=False)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
