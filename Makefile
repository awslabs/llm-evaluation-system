.PHONY: dev stop logs restart build clean keys release release-minor release-major _release

COMPOSE := docker compose -f local/compose.yaml

dev:             ## Build the SPA + start all services
	@eval $$(aws configure export-credentials --format env 2>/dev/null) && \
	export AWS_REGION=$${AWS_REGION:-$$(aws configure get region 2>/dev/null)} && \
	if [ -z "$$AWS_ACCESS_KEY_ID" ]; then \
	  echo "Error: No AWS credentials. Run: aws sso login"; exit 1; \
	fi && \
	export AWS_DEFAULT_REGION=$$AWS_REGION && \
	if [ -f .env.keys ]; then set -a; . ./.env.keys; set +a; fi && \
	echo "Building frontend bundle (nginx serves frontend/dist)..." && \
	(cd frontend && npm run build) && \
	$(COMPOSE) up --build

dev-spa:         ## Rebuild just the SPA bundle (nginx picks it up; no restart needed)
	@cd frontend && npm run build

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

sync-pricing:    ## Refresh the vendored LiteLLM pricing snapshot (review the diff!)
	@python scripts/sync-pricing.py

release:         ## Tag a patch release and push (triggers PyPI publish)
	@$(MAKE) _release BUMP=patch

release-minor:   ## Tag a minor release and push
	@$(MAKE) _release BUMP=minor

release-major:   ## Tag a major release and push
	@$(MAKE) _release BUMP=major

# Version lives in git tags. We read the latest v* tag, bump the requested
# component, push the new tag — and that's it. No source edits, no
# "Release vX.Y.Z" commits. The publish.yml workflow runs on tag push and
# setuptools-scm reads the version straight from the tag at build time.
_release:
	@if [ -n "$$(git status --porcelain)" ]; then \
	  echo "Working tree is dirty. Commit or stash first."; exit 1; \
	fi
	@if [ "$$(git rev-parse --abbrev-ref HEAD)" != "main" ]; then \
	  echo "Release must be run from main (currently on $$(git rev-parse --abbrev-ref HEAD))."; exit 1; \
	fi
	@git pull --ff-only origin main
	@git fetch --tags origin
	@OLD=$$(git tag -l 'v*' | sed 's/^v//' | sort -V | tail -n1) && \
	if [ -z "$$OLD" ]; then OLD="0.0.0"; fi && \
	NEW=$$(python3 -c "v='$$OLD'.split('.'); part='$(BUMP)'; \
	  idx={'major':0,'minor':1,'patch':2}[part]; v[idx]=str(int(v[idx])+1); \
	  [v.__setitem__(i,'0') for i in range(idx+1,3)]; print('.'.join(v))") && \
	echo "Releasing v$$NEW (previous: v$$OLD)" && \
	git tag "v$$NEW" && \
	git push origin "v$$NEW" && \
	echo "Pushed v$$NEW. Watch https://github.com/awslabs/llm-evaluation-system/actions"
