# /opt/invoice-ocr/ops/Makefile — operator shortcuts
#
# Designed for SSH'd-in humans, not CI.  CI uses the .sh scripts directly.
#
# Usage:  cd /opt/invoice-ocr && make <target>

.PHONY: help ps logs logs-api logs-worker status rollback backup restart \
        pull-secrets clean-images health sh-api sh-worker

help: ## list targets
	@awk 'BEGIN{FS=":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}' $(MAKEFILE_LIST)

ps: ## docker compose ps
	docker compose ps

logs: ## tail all compose logs
	docker compose logs -f --tail=100

logs-api: ## tail api logs
	docker compose logs -f --tail=200 api

logs-worker: ## tail worker logs
	docker compose logs -f --tail=200 worker

status: ## system snapshot (ps + readyz + image)
	@docker compose ps
	@echo
	@echo "-- /readyz --"; curl -sS http://localhost:8000/readyz || true; echo
	@echo "-- IMAGE --"; grep ^IMAGE .env || echo "(no IMAGE pin)"
	@echo "-- previous --"; cat .previous_sha 2>/dev/null || echo "(none)"

restart: ## recreate all containers from current .env
	docker compose up -d --remove-orphans

rollback: ## flip back to .previous_sha
	@bash /opt/invoice-ocr/ops/rollback.sh

backup: ## trigger on-demand EBS snapshot
	@ENV=$$(grep ^ENV= .env | cut -d= -f2) bash /opt/invoice-ocr/ops/snapshot.sh

pull-secrets: ## re-sync .env from SSM
	@ENV=$$(grep ^ENV= .env | cut -d= -f2) bash /opt/invoice-ocr/ops/pull-secrets.sh

clean-images: ## docker image prune (dangling only)
	docker image prune -f

health: ## smoke the API from inside the box
	@curl -sS -i http://localhost:8000/healthz | head -5
	@echo
	@curl -sS -i http://localhost:8000/readyz  | head -5

sh-api: ## shell into a running api container
	docker compose exec api bash

sh-worker: ## shell into worker-1
	docker compose exec worker bash
