SHELL := /bin/bash
.DEFAULT_GOAL := help

SCRIPTS := scripts

.PHONY: help check plan bootstrap deploy verify status render destroy

help: ## Show available targets
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "%-12s %s\n", $$1, $$2}'

check: ## Check local tools, context, and required environment
	@$(SCRIPTS)/check.sh
	@bash -n $(SCRIPTS)/*.sh

plan: ## Validate manifests and render Helm charts without changing the cluster
	@$(SCRIPTS)/plan.sh

bootstrap: ## Install/upgrade the complete lab stack (requires secrets in .env)
	@$(SCRIPTS)/bootstrap.sh

deploy: bootstrap ## Alias for bootstrap

verify: ## Verify pods, Gateway API status, VIPs, and HTTP routes
	@$(SCRIPTS)/verify.sh

status: ## Show the lab cluster status
	@$(SCRIPTS)/status.sh

render: ## Render generated Gateway manifests into rendered/
	@$(SCRIPTS)/render.sh

destroy: ## Destructive: remove this lab stack only when CONFIRM=destroy is set
	@$(SCRIPTS)/destroy.sh
