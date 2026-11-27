from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import logging
from pathlib import Path
import time
from typing import Any

logger = logging.getLogger("hub_spoke.planner")


class TopologyError(Exception):
    pass


def sign_request(secret: str, timestamp_unix: int, body: bytes) -> str:
    msg = f"{timestamp_unix}.".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_request(
    secret: str,
    signature: str | None,
    timestamp: str | None,
    body: bytes,
    max_skew_sec: int = 300,
    now_unix: int | None = None,
) -> tuple[bool, str]:
    if not signature or not timestamp:
        return False, "missing X-Signature-256 or X-Timestamp header"
    try:
        ts = int(timestamp.strip())
    except ValueError:
        return False, "invalid X-Timestamp header"

    # Evaluate in UTC unix time so Daylight Saving Time transitions never shift replay windows.
    now = int(time.time()) if now_unix is None else now_unix
    if abs(now - ts) > max_skew_sec:
        return False, "request timestamp outside replay window"

    expected = sign_request(secret, ts, body)
    if not hmac.compare_digest(expected, signature.strip()):
        return False, "invalid hmac signature"
    return True, "ok"


def _retryable_aws(exc: Any) -> bool:
    resp = getattr(exc, "response", {}) or {}
    code = resp.get("Error", {}).get("Code", "")
    status = resp.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
    return code in {
        "Throttling",
        "ThrottlingException",
        "RequestLimitExceeded",
        "TooManyRequestsException",
    } or status in {429, 502, 503, 504}


def _call_aws(op: str, fn: Any, exc_cls: Any, attempts: int = 4) -> Any:
    backoff = 0.1
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except exc_cls as exc:
            if not _retryable_aws(exc) or attempt == attempts:
                raise
            # Cap per-retry delay at 2.0s so HTTP route-sync calls do not time out under bursts.
            pause = min(backoff, 2.0)
            logger.warning("retrying aws op=%s attempt=%d pause_s=%.2f err=%s", op, attempt, pause, exc)
            time.sleep(pause)
            backoff *= 2.0


def load_hub_definitions(topologies_dir: Path) -> list[dict[str, Any]]:
    hubs: list[dict[str, Any]] = []
    for path in sorted(topologies_dir.rglob("*.json")):
        hubs.append(json.loads(path.read_text(encoding="utf-8")))
    return hubs


def detect_cidr_overlaps(hubs: list[dict[str, Any]]) -> list[dict[str, str]]:
    seen: list[tuple[str, str, ipaddress.IPv4Network | ipaddress.IPv6Network]] = []
    overlaps: list[dict[str, str]] = []
    for hub in hubs:
        hid = hub.get("hub_id", "unknown")
        for spoke in hub.get("spokes", []):
            sid = spoke.get("spoke_id", "unknown")
            cidr_raw = spoke.get("cidr", "")
            net = ipaddress.ip_network(cidr_raw, strict=False)
            for prev_hub, prev_spoke, prev_net in seen:
                if net.overlaps(prev_net):
                    overlaps.append({
                        "left": f"{prev_hub}/{prev_spoke} ({prev_net})",
                        "right": f"{hid}/{sid} ({net})",
                    })
            seen.append((hid, sid, net))
    return overlaps


class HubSpokeSyncer:
    def __init__(
        self,
        topologies_dir: Path,
        region: str = "us-east-1",
        endpoint_url: str | None = "http://localhost:4566",
        bucket: str = "hub-spoke-topology-snapshots",
        ec2_client: Any | None = None,
        s3_client: Any | None = None,
        client_error_cls: Any | None = None,
    ) -> None:
        self.hubs = load_hub_definitions(topologies_dir)
        self.region = region
        self.bucket = bucket

        if client_error_cls is not None:
            self._client_error = client_error_cls
            boto3_mod = None
        else:
            try:
                import boto3
                from botocore.exceptions import ClientError

                boto3_mod = boto3
                self._client_error = ClientError
            except ImportError:
                boto3_mod = None

                class _FallbackError(Exception):
                    response: dict[str, Any] = {}

                self._client_error = _FallbackError

        client_kwargs: dict[str, Any] = {"region_name": region}
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url
            client_kwargs["aws_access_key_id"] = "test"
            client_kwargs["aws_secret_access_key"] = "test"

        if ec2_client is not None:
            self.ec2 = ec2_client
        else:
            if boto3_mod is None:
                raise RuntimeError("boto3 is required when ec2_client is not supplied")
            self.ec2 = boto3_mod.client("ec2", **client_kwargs)

        if s3_client is not None:
            self.s3 = s3_client
        else:
            if boto3_mod is None:
                raise RuntimeError("boto3 is required when s3_client is not supplied")
            self.s3 = boto3_mod.client("s3", **client_kwargs)

    def _ensure_bucket(self) -> None:
        try:
            self.s3.head_bucket(Bucket=self.bucket)
        except self._client_error:
            try:
                self.s3.create_bucket(Bucket=self.bucket)
            except self._client_error as exc:
                code = (getattr(exc, "response", {}) or {}).get("Error", {}).get("Code", "")
                if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                    raise

    def plan_and_publish(
        self,
        correlation_id: str,
        window_extension_minutes: int = 0,
        now_utc: datetime | None = None,
    ) -> dict[str, Any]:
        overlaps = detect_cidr_overlaps(self.hubs)
        if overlaps:
            raise TopologyError(f"overlapping cidrs detected: {overlaps}")

        aws_vpcs = _call_aws(
            "ec2:describe_vpcs",
            lambda: self.ec2.describe_vpcs(),
            self._client_error,
        )
        observed_vpc_ids = [
            v.get("VpcId", "") for v in aws_vpcs.get("Vpcs", []) if v.get("VpcId")
        ]

        routes: list[dict[str, Any]] = []
        for hub in self.hubs:
            for spoke in hub.get("spokes", []):
                routes.append({
                    "hub_id": hub["hub_id"],
                    "fabric": hub["fabric"],
                    "spoke_id": spoke["spoke_id"],
                    "cidr": spoke["cidr"],
                    "zone": spoke["zone"],
                    "next_hop": hub["hub_id"],
                })

        ts_utc = now_utc or datetime.now(timezone.utc)
        snapshot = {
            "correlation_id": correlation_id,
            "generated_at_utc": ts_utc.isoformat(),
            "region": self.region,
            "window_extension_minutes": window_extension_minutes,
            "hub_count": len(self.hubs),
            "route_count": len(routes),
            "aws_observed_vpcs": observed_vpc_ids,
            "routes": routes,
        }

        self._ensure_bucket()
        key = f"snapshots/{ts_utc.strftime('%Y-%m-%d')}/{correlation_id}.json"
        _call_aws(
            "s3:put_object",
            lambda: self.s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=json.dumps(snapshot, indent=2).encode("utf-8"),
                ContentType="application/json",
            ),
            self._client_error,
        )

        return {
            "s3_bucket": self.bucket,
            "s3_key": key,
            "snapshot": snapshot,
        }
