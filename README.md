# hub-spoke-tgw-vwan-ncc

Python FastAPI service and route planner for hub-and-spoke topologies across AWS Transit Gateway (`10.10.0.0/16`), Azure vWAN (`10.20.0.0/16`), and GCP NCC (`10.30.0.0/16`) with CIDR overlap checks, OPA route/window policies, and S3 snapshot publishing.

## Prerequisites

- Python 3.10+
- Docker and Docker Compose
- `curl` and `openssl`

## Local Setup

Start LocalStack (`ec2,s3`) and OPA:

```bash
make up
```

Run the API server:

```bash
make run
```

Run tests and benchmark:

```bash
make test
make bench
```

Stop containers:

```bash
make down
```

## Sample Request

```bash
BODY='{"actor_role":"net-eng","source_hub":"tgw-core-use1","target_hub":"vwan-hub-eastus","source_zone":"shared","cross_hub_bridge":true}'
TS=$(date -u +%s)
SIG="sha256=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac 'local-dev-hub-spoke-secret' | awk '{print $2}')"

curl -s -X POST http://localhost:8080/v1/topology/sync   -H "Content-Type: application/json"   -H "X-Timestamp: $TS"   -H "X-Signature-256: $SIG"   -d "$BODY"
```

## Known Limitations

- AWS EC2 and S3 use `boto3`; Azure vWAN and GCP NCC hubs are defined declaratively in `topologies/`.
- Single AWS region per process (`AWS_REGION`).
- `describe_vpcs` does not iterate over `NextToken` pagination tokens.
