.PHONY: dev stop logs restart build clean keys

COMPOSE := docker compose -f local/compose.yaml

dev:             ## Start all services with hot reload
	@eval $$(aws configure export-credentials --format env 2>/dev/null) && \
	export AWS_REGION=$${AWS_REGION:-$$(aws configure get region 2>/dev/null)} && \
	if [ -z "$$AWS_ACCESS_KEY_ID" ]; then \
	  echo "Error: No AWS credentials. Run: aws sso login"; exit 1; \
	fi && \
	export AWS_DEFAULT_REGION=$$AWS_REGION && \
	if [ -f .env.keys ]; then set -a; . ./.env.keys; set +a; fi && \
	$(COMPOSE) up --build

keys:            ## Configure external provider API keys (optional)
	@echo "# External LLM provider API keys (optional)" > .env.keys.tmp
	@echo "# Uncomment and set keys for providers you want to use" >> .env.keys.tmp
	@echo "" >> .env.keys.tmp
	@echo "#OPENAI_API_KEY=" >> .env.keys.tmp
	@echo "#ANTHROPIC_API_KEY=" >> .env.keys.tmp
	@echo "#GOOGLE_API_KEY=" >> .env.keys.tmp
	@if [ -f .env.keys ]; then \
	  echo "Existing .env.keys found. Edit it directly or replace:"; \
	  echo "  mv .env.keys.tmp .env.keys"; \
	else \
	  mv .env.keys.tmp .env.keys; \
	  echo "Created .env.keys — edit it to add your API keys."; \
	  echo "This file is gitignored and never committed."; \
	fi
	@rm -f .env.keys.tmp

stop:            ## Stop all services
	@$(COMPOSE) down

logs:            ## Tail logs (all or one: make logs s=backend)
	@$(COMPOSE) logs -f $(s)

restart:         ## Restart with fresh creds (all or one: make restart s=backend)
	@eval $$(aws configure export-credentials --format env 2>/dev/null) && \
	export AWS_REGION=$${AWS_REGION:-$$(aws configure get region 2>/dev/null)} && \
	export AWS_DEFAULT_REGION=$$AWS_REGION && \
	$(COMPOSE) up -d $(s)

build:           ## Build all images
	@$(COMPOSE) build

clean: stop      ## Stop and remove volumes
	@$(COMPOSE) down -v
