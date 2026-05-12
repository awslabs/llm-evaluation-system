.PHONY: dev stop logs restart build clean keys release release-minor release-major _release

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

release:         ## Bump patch version, tag, and push (triggers PyPI publish)
	@$(MAKE) _release BUMP=patch

release-minor:   ## Bump minor version, tag, and push
	@$(MAKE) _release BUMP=minor

release-major:   ## Bump major version, tag, and push
	@$(MAKE) _release BUMP=major

_release:
	@if [ -n "$$(git status --porcelain)" ]; then \
	  echo "Working tree is dirty. Commit or stash first."; exit 1; \
	fi
	@if [ "$$(git rev-parse --abbrev-ref HEAD)" != "main" ]; then \
	  echo "Release must be run from main (currently on $$(git rev-parse --abbrev-ref HEAD))."; exit 1; \
	fi
	@git pull --ff-only origin main
	@OLD=$$(python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])") && \
	NEW=$$(python3 -c "v='$$OLD'.split('.'); part='$(BUMP)'; \
	  idx={'major':0,'minor':1,'patch':2}[part]; v[idx]=str(int(v[idx])+1); \
	  [v.__setitem__(i,'0') for i in range(idx+1,3)]; print('.'.join(v))") && \
	echo "Bumping $$OLD -> $$NEW" && \
	python3 -c "import re,pathlib; p=pathlib.Path('pyproject.toml'); p.write_text(re.sub(r'^version = \".*\"', 'version = \"'+'$$NEW'+'\"', p.read_text(), count=1, flags=re.M))" && \
	git add pyproject.toml && \
	git commit -m "Release v$$NEW" && \
	git tag "v$$NEW" && \
	git push origin main && \
	git push origin "v$$NEW" && \
	echo "Pushed v$$NEW. Watch https://github.com/awslabs/llm-evaluation-system/actions"
