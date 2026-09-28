# Local verification targets. See README.md "本地验证" for expected output.

GO ?= go
PKG := ./...

.PHONY: all build test test-race test-verbose vet fmt check smoke clean

all: check build

build:
	$(GO) build -o bin/placer ./cmd/placer

# Full test suite (unit + independent-oracle differential + e2e).
test:
	$(GO) test $(PKG)

test-verbose:
	$(GO) test -v $(PKG)

# Race detector for the store/loop concurrency paths.
test-race:
	$(GO) test -race $(PKG)

vet:
	$(GO) vet $(PKG)

fmt:
	@test -z "$$(gofmt -l . | tee /dev/stderr)"

check: fmt vet test

# End-to-end smoke against a real binary over HTTP.
smoke: build
	./scripts/smoke.sh

clean:
	rm -rf bin data *.db *.db-wal *.db-shm
