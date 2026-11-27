from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
import httpx

from src.hub_spoke.planner import HubSpokeSyncer, TopologyError, load_hub_definitions, verify_request

app = FastAPI(title="hub-spoke-tgw-vwan-ncc", version="0.1.0")


def _syncer() -> HubSpokeSyncer:
    root = Path(__file__).resolve().parents[2]
    return HubSpokeSyncer(
        topologies_dir=root / "topologies",
        region=os.getenv("AWS_REGION", "us-east-1"),
        endpoint_url=os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566"),
        bucket=os.getenv("HUB_SPOKE_BUCKET", "hub-spoke-topology-snapshots"),
    )


def _eval_opa(opa_url: str, opa_input: dict[str, Any]) -> tuple[bool, str]:
    base = opa_url.rstrip("/")
    with httpx.Client(timeout=3.0) as client:
        iso_r = client.post(f"{base}/v1/data/hubspoke/isolation", json={"input": opa_input})
        iso_r.raise_for_status()
        iso_res = iso_r.json().get("result") or {}
        if not iso_res.get("allow", False):
            return False, iso_res.get("reason", "denied by isolation policy")

        win_r = client.post(f"{base}/v1/data/hubspoke/window", json={"input": opa_input})
        win_r.raise_for_status()
        win_res = win_r.json().get("result") or {}
        if not win_res.get("allow", False):
            return False, win_res.get("reason", "denied by change window policy")

        return True, win_res.get("reason", "allowed")


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    hubs = [h.get("hub_id") for h in load_hub_definitions(root / "topologies")]
    return {
        "status": "ok",
        "hubs": hubs,
        "utc_time": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/v1/topology/sync")
async def sync_topology(
    request: Request,
    x_signature_256: str | None = Header(default=None, alias="X-Signature-256"),
    x_timestamp: str | None = Header(default=None, alias="X-Timestamp"),
    x_correlation_id: str | None = Header(default=None, alias="X-Correlation-ID"),
) -> dict[str, Any]:
    raw_body = await request.body()
    secret = os.getenv("WEBHOOK_HMAC_SECRET", "local-dev-hub-spoke-secret")
    ok, reason = verify_request(secret, x_signature_256, x_timestamp, raw_body)
    if not ok:
        raise HTTPException(status_code=401, detail={"error": reason})

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "malformed json payload"})

    now_utc = datetime.now(timezone.utc)
    corr_id = x_correlation_id or payload.get("correlation_id") or f"hs-{int(now_utc.timestamp())}"
    win_ext = payload.get("window_extension") or {}
    ext_mins = int(win_ext.get("extra_minutes", 0)) if win_ext.get("enabled") else 0

    opa_input = {
        "actor_role": payload.get("actor_role", "net-bot"),
        "source_hub": payload.get("source_hub", "tgw-core-use1"),
        "target_hub": payload.get("target_hub", "tgw-core-use1"),
        "source_zone": payload.get("source_zone", "shared"),
        "cross_hub_bridge": bool(payload.get("cross_hub_bridge", False)),
        "utc_hour": now_utc.hour,
        "window_extension": win_ext,
    }

    opa_url = os.getenv("OPA_URL", "http://localhost:8181")
    try:
        allowed, policy_reason = _eval_opa(opa_url, opa_input)
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"error": f"opa check failed: {exc}"})

    if not allowed:
        raise HTTPException(status_code=403, detail={"correlation_id": corr_id, "status": "denied", "reason": policy_reason})

    try:
        published = _syncer().plan_and_publish(correlation_id=corr_id, window_extension_minutes=ext_mins, now_utc=now_utc)
    except TopologyError as exc:
        raise HTTPException(status_code=409, detail={"error": str(exc)})
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"error": f"topology sync failed: {exc}"})

    return {
        "correlation_id": corr_id,
        "status": "published",
        "policy_reason": policy_reason,
        "published": published,
    }
