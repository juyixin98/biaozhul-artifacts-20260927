.PHONY: all build run test test-race vet fmt demo tidy clean

GO ?= go
BIN := bin/fieldmerged
ADDR ?= :8080
DATA ?= ./data

all: build

build:
	$(GO) build -mod=vendor -o $(BIN) ./cmd/fieldmerged

run: build
	$(BIN) -addr $(ADDR) -data $(DATA)

test:
	$(GO) test -mod=vendor ./... -count=1

test-race:
	$(GO) test -mod=vendor -race ./... -count=1

vet:
	$(GO) vet ./...

fmt:
	$(GO) fmt ./...

tidy:
	$(GO) mod tidy && $(GO) mod vendor

demo:
	./scripts/demo.sh http://localhost:8080

clean:
	rm -rf bin data testlogs
