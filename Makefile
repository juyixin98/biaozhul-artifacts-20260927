# natlab Makefile — local synthetic lab only; nothing here touches real networks.

GO      ?= go
BIN     := bin/natlab
DB      := ./natlab.db
REPORTS := reports

.PHONY: all build test unit integration race cover fmt vet tidy \
        fixtures serve clean report

all: build

build:
	$(GO) build -o $(BIN) ./cmd/natlab

unit:
	$(GO) test -count=1 ./internal/...

integration:
	$(GO) test -count=1 ./tests/...

test: unit integration

race:
	$(GO) test -race -count=1 ./...

cover:
	mkdir -p $(REPORTS)
	$(GO) test -count=1 -coverpkg=./... -coverprofile=$(REPORTS)/coverage.out ./...
	$(GO) tool cover -func=$(REPORTS)/coverage.out | tail -1

fmt:
	$(GO) fmt ./...

vet:
	$(GO) vet ./...

tidy:
	$(GO) mod tidy

# Replay every shipped fixture and store the full decision logs (run id,
# intermediate states, rationale) so a problem can be replayed later.
fixtures: build
	mkdir -p $(REPORTS)
	@for f in traces/*.json; do \
	  name=$$(basename $$f .json); \
	  echo "replaying $$f"; \
	  $(BIN) replay --config configs/natlab.json --trace $$f \
	    --out $(REPORTS)/$$name.json >/dev/null || true; \
	done
	@echo "reports written to $(REPORTS)/"

serve: build
	$(BIN) serve --config configs/natlab.json

report: test cover fixtures
	@echo "full report bundle in $(REPORTS)/"

clean:
	rm -rf $(BIN) $(REPORTS) $(DB)
