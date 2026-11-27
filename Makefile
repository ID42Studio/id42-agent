.PHONY: init build up down restart logs status doctor test

COMPOSE ?= docker compose

init:
	./scripts/bootstrap.sh

build:
	$(COMPOSE) build nanobot-upstream nanobot-gateway

up: init
	$(COMPOSE) up -d nanobot-gateway

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart nanobot-gateway

logs:
	$(COMPOSE) logs -f nanobot-gateway

status:
	$(COMPOSE) --profile cli run --rm --no-deps -T nanobot-cli

doctor:
	./scripts/doctor.sh

test:
	./scripts/smoke-test.sh
