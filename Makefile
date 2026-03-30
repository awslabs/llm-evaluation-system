.PHONY: check-creds dev build run stop clean logs

RUNTIME := $(shell command -v podman 2>/dev/null || command -v docker 2>/dev/null)
IMAGE := managed-eval
CONTAINER := managed-eval-local

# Podman VMs on macOS lose track of time after sleep. AWS rejects requests
# when the clock is >5 min off. Force an NTP correction before running.
define SYNC_CLOCK
	if command -v podman >/dev/null 2>&1 && podman machine inspect >/dev/null 2>&1; then \
	  podman machine ssh sudo chronyc makestep >/dev/null 2>&1 || true; \
	fi
endef

# Resolve AWS credentials: Bedrock API key OR IAM (not both).
define CHECK_CREDS
	if [ -n "$$AWS_BEARER_TOKEN_BEDROCK" ]; then \
	  unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN; \
	else \
	  eval $$(aws configure export-credentials --format env 2>/dev/null); \
	  export AWS_REGION=$${AWS_REGION:-$$(aws configure get region 2>/dev/null)}; \
	  if [ -z "$$AWS_ACCESS_KEY_ID" ]; then \
	    echo "Error: No AWS credentials found."; \
	    echo "  Option 1: AWS_PROFILE=my-profile make $(MAKECMDGOALS)"; \
	    echo "  Option 2: AWS_BEARER_TOKEN_BEDROCK=your-key make $(MAKECMDGOALS)"; \
	    exit 1; \
	  fi; \
	fi
endef

check-creds:     ## Verify AWS credentials are available
	@$(CHECK_CREDS)

dev: check-creds build  ## Dev mode with hot reload (volume mounts + auto-reload)
	@$(RUNTIME) stop $(CONTAINER) 2>/dev/null || true; \
	$(RUNTIME) rm $(CONTAINER) 2>/dev/null || true; \
	$(SYNC_CLOCK); \
	$(CHECK_CREDS); \
	$(RUNTIME) run --rm --name $(CONTAINER) \
	  -p 4001:4001 \
	  -v $(PWD)/backend:/app/backend \
	  -v $(PWD)/local:/app/local \
	  -v $(PWD)/frontend:/app/frontend-src \
	  -v eval-frontend-nm:/app/frontend-src/node_modules \
	  -v eval-data:/data \
	  -e DEV_MODE=true \
	  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
	  -e AWS_SESSION_TOKEN -e AWS_REGION \
	  -e AWS_BEDROCK_REGION \
	  -e AWS_BEARER_TOKEN_BEDROCK \
	  $(IMAGE)

build:           ## Build container image
	$(RUNTIME) build -f docker/local/Dockerfile -t $(IMAGE) .

run: check-creds build  ## Run production mode (no volume mounts, built assets)
	@$(RUNTIME) stop $(CONTAINER) 2>/dev/null || true; \
	$(RUNTIME) rm $(CONTAINER) 2>/dev/null || true; \
	$(SYNC_CLOCK); \
	$(CHECK_CREDS); \
	$(RUNTIME) run -d --name $(CONTAINER) \
	  -p 4001:4001 \
	  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY \
	  -e AWS_SESSION_TOKEN -e AWS_REGION \
	  -e AWS_BEDROCK_REGION \
	  -e AWS_BEARER_TOKEN_BEDROCK \
	  -v eval-data:/data \
	  $(IMAGE)

stop:            ## Stop container
	$(RUNTIME) stop $(CONTAINER) 2>/dev/null || true
	$(RUNTIME) rm $(CONTAINER) 2>/dev/null || true

clean: stop      ## Stop and remove build caches (preserves data)
	$(RUNTIME) volume rm eval-frontend-nm 2>/dev/null || true

logs:            ## Tail container logs
	$(RUNTIME) logs -f $(CONTAINER)
