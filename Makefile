# flowrouter Makefile
# All Go commands pin GOTOOLCHAIN=local so the build uses the installed
# toolchain declared in go.mod (go 1.23) instead of auto-downloading a newer one.

GO ?= go
export GOTOOLCHAIN := local

.PHONY: all build test race cover vet fixtures fmt tidy run check-config clean

all: build

build:
	$(GO) build ./...

## Generate the fixed flow-set fixtures (deterministic for the pinned seed).
fixtures:
	$(GO) run ./cmd/genfixtures --seed 20260927

## Regenerate golden reference files with the independent Python oracle.
## The oracle is a separate language/implementation and never imports Go code.
oracle:
	python3 testdata/oracle/oracle.py --repo-root . --flowset flows_smoke
	python3 testdata/oracle/oracle.py --repo-root . --flowset flows_10k

test:
	$(GO) test ./... -count=1

race:
	$(GO) test ./... -race -count=1

cover:
	$(GO) test ./... -count=1 -cover

vet:
	$(GO) vet ./...

fmt:
	$(GO) fmt ./...

tidy:
	$(GO) mod tidy

run:
	$(GO) run ./cmd/flowrouter -config configs/config.example.yaml -flowsets testdata/flowsets -reset-db

check-config:
	$(GO) run ./cmd/flowrouter -config configs/config.example.yaml -check-config

clean:
	rm -rf data /tmp/flowrouter
