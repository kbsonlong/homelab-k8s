IMG ?= gitea.kbsonlong.com/kbsonlong/waf-console
TAG ?= $(shell git rev-parse --short HEAD)

.PHONY: tidy test build run image blocker
tidy:   ; go mod tidy
test:   ; go test ./...
build:  ; CGO_ENABLED=0 go build -o bin/waf-console ./cmd/waf-console
blocker:; CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o bin/waf-blocker ./cmd/waf-blocker
run:    ; GIT_TOKEN=$${GIT_TOKEN} go run ./cmd/waf-console
image:  ; docker buildx build --platform linux/arm64,linux/amd64 -t $(IMG):$(TAG) --push .
