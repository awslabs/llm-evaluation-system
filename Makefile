.PHONY: dev stop logs restart build clean creds

COMPOSE := docker compose -f local/compose.yaml

dev:             ## Start all services with hot reload
	@eval $$(aws configure export-credentials --format env 2>/dev/null) && \
	export AWS_REGION=$${AWS_REGION:-$$(aws configure get region 2>/dev/null)} && \
	if [ -z "$$AWS_ACCESS_KEY_ID" ]; then \
	  echo "Error: No AWS credentials. Run: aws sso login"; exit 1; \
	fi && \
	export AWS_DEFAULT_REGION=$$AWS_REGION && \
	$(COMPOSE) up --build

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
