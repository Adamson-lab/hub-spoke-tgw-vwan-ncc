import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.hub_spoke.planner import (
    HubSpokeSyncer,
    TopologyError,
    detect_cidr_overlaps,
    load_hub_definitions,
    sign_request,
    verify_request,
)


class TestHubSpokePlanner(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.fake_ec2 = MagicMock()
        self.fake_ec2.describe_vpcs.return_value = {"Vpcs": [{"VpcId": "vpc-111"}]}
        self.fake_s3 = MagicMock()
        self.syncer = HubSpokeSyncer(
            topologies_dir=self.root / "topologies",
            region="us-east-1",
            endpoint_url=None,
            bucket="test-hub-spoke-bucket",
            ec2_client=self.fake_ec2,
            s3_client=self.fake_s3,
        )

    def test_loads_three_fabrics_and_publishes_snapshot(self) -> None:
        res = self.syncer.plan_and_publish(correlation_id="corr-51", window_extension_minutes=15)
        self.assertEqual(res["snapshot"]["hub_count"], 3)
        self.assertEqual(res["snapshot"]["route_count"], 6)
        self.assertEqual(res["snapshot"]["aws_observed_vpcs"], ["vpc-111"])
        self.fake_s3.put_object.assert_called_once()
        written = json.loads(self.fake_s3.put_object.call_args.kwargs["Body"].decode("utf-8"))
        self.assertEqual(written["window_extension_minutes"], 15)

    def test_detects_overlapping_cidrs(self) -> None:
        hubs = load_hub_definitions(self.root / "topologies")
        self.assertEqual(detect_cidr_overlaps(hubs), [])

        hubs[1]["spokes"][0]["cidr"] = "10.10.1.0/24"
        self.syncer.hubs = hubs
        with self.assertRaises(TopologyError):
            self.syncer.plan_and_publish(correlation_id="corr-overlap")

    def test_hmac_signing_and_skew_check(self) -> None:
        secret = "hs-secret"
        body = b'{"source_hub":"tgw-core-use1","target_hub":"vwan-hub-eastus"}'
        now = 1764267000
        sig = sign_request(secret, now, body)
        ok, _ = verify_request(secret, sig, str(now), body, now_unix=now)
        self.assertTrue(ok)

        bad_ok, _ = verify_request(secret, sig, str(now - 500), body, now_unix=now)
        self.assertFalse(bad_ok)

    def test_bench_signature_verify(self) -> None:
        secret = "bench-secret"
        body = b'{"source_hub":"tgw-core-use1","target_hub":"ncc-hub-useast1"}'
        now = int(time.time())
        sig = sign_request(secret, now, body)
        iters = 5000
        t0 = time.perf_counter()
        for _ in range(iters):
            ok, _ = verify_request(secret, sig, str(now), body, now_unix=now)
            self.assertTrue(ok)
        ms = (time.perf_counter() - t0) * 1000.0
        print(f"BENCH verify_request: {iters} calls in {ms:.2f} ms ({(ms / iters) * 1000:.2f} us/call)")


if __name__ == "__main__":
    unittest.main()
