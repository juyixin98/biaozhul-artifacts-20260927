# natlab - local replayable stateful NAT model
.POSIX:

GO ?= go
GOFLAGS ?= -mod=mod
BIN ?= bin/natlab
CONFIG ?= configs/natlab.json
FIXTURE ?= testdata/fixtures/01_port_exhaustion.json

.PHONY: all build test test-race vet fmt check clean replay serve tidy

all: build

build:
	$(GO) build -o $(BIN) ./cmd/natlab

test:
	$(GO) test ./...

test-race:
	$(GO) test -race ./...

vet:
	$(GO) vet ./...

fmt:
	$(GO) fmt ./...

check: vet test-race

replay:
	$(BIN) replay -config $(CONFIG) -fixture $(FIXTURE) -mem

serve: build
	$(BIN) serve -config $(CONFIG)

tidy:
	$(GO) mod tidy

clean:
	rm -rf bin natlab.db logs/*.log testdata/runs/*/*.log
