"""FastAPI app: one authenticated endpoint that serves BOTH clients.

  POST /event   -- iPhone Shortcuts JSON  OR  OwnTracks transition JSON.
                   Auth: shared secret in the `X-Auth-Token` header.
  GET  /health  -- unauthenticated liveness probe (no data leaked).

Auth failure => 401 and NOTHING is stored (a public endpoint on the open
internet must not let random traffic fill the disk).

For an AUTHENTICATED request we store the raw body immutably BEFORE any
processing (spec: "store every raw event exactly as received, before any
processing"). If the body is valid JSON but doesn't normalize, the raw is still
retained (so you can re-derive later) and we return 202.
"""

from __future__ import annotations

import hashlib
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import load_config
from .db import connect, init_db
from .ingest.normalize import detect_client
from .pipeline import ingest, now_utc_iso, store_raw

app = FastAPI(title="geofence-ingest", version="0.1.0")

_config = load_config()
if not _config.secret:
    raise RuntimeError(
        "GEOFENCE_SECRET is not set. Refusing to start an unauthenticated "
        "public endpoint. Set it in the systemd EnvironmentFile."
    )

# Ensure schema exists on boot.
_boot = connect(_config.db_path)
init_db(_boot)
_boot.close()


def _get_conn():
    return connect(_config.db_path)


def _client_ip(request: Request) -> str | None:
    # Trust X-Forwarded-For only for the left-most hop if present (behind a
    # reverse proxy); otherwise the socket peer.
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/event")
async def event(request: Request):
    # 1. Auth first. Bad/missing secret => reject, store nothing.
    token = request.headers.get("x-auth-token")
    if not token or token != _config.secret:
        return JSONResponse({"status": "unauthorized"}, status_code=401)

    body = await request.body()
    text = body.decode("utf-8", errors="replace")
    ip = _client_ip(request)

    conn = _get_conn()
    try:
        # 2. Parse JSON. Unparseable but authenticated => still store raw.
        try:
            payload = json.loads(text)
        except Exception:
            dedupe = "raw|" + hashlib.sha256(text.encode()).hexdigest()
            store_raw(conn, dedupe, "unknown", text, ip, now_utc_iso())
            conn.commit()
            return JSONResponse({"status": "stored_unparsed"}, status_code=202)

        # 3. Normalize + ingest. If it doesn't normalize, retain the raw.
        try:
            result = ingest(conn, _config, payload, text, ip)
        except ValueError as e:
            dedupe = "raw|" + hashlib.sha256(text.encode()).hexdigest()
            store_raw(conn, dedupe, detect_client(payload), text, ip, now_utc_iso())
            conn.commit()
            return JSONResponse(
                {"status": "stored_unprocessed", "detail": str(e)}, status_code=202
            )

        return result
    finally:
        conn.close()
