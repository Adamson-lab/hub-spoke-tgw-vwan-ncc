SHELL := /bin/bash
COMPOSE_FILE := deploy/docker-compose.yml

.PHONY: up down run test bench lint

up:
	docker compose -f $(COMPOSE_FILE) up -d --wait

down:
	docker compose -f $(COMPOSE_FILE) down -v

lint:
	python3 -m py_compile src/hub_spoke/planner.py src/hub_spoke/api.py tests/test_hub_spoke.py

run:
	AWS_ENDPOINT_URL=http://localhost:4566 \
	OPA_URL=http://localhost:8181 \
	HUB_SPOKE_BUCKET=hub-spoke-topology-snapshots \
	uvicorn src.hub_spoke.api:app --host 0.0.0.0 --port 8080

test:
	AWS_ENDPOINT_URL=http://localhost:4566 \
	OPA_URL=http://localhost:8181 \
	HUB_SPOKE_BUCKET=hub-spoke-topology-snapshots \
	python3 -m unittest discover -s tests -p "test_*.py" -v

bench:
	python3 -m unittest tests/test_hub_spoke.py -k bench -v
