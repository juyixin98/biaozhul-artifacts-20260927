.PHONY: all build vet test test-fast acceptance reproduce clean

all: build vet test

build:
	go build ./...

vet:
	go vet ./...

# Fast in-process suite (unit, fault injection, store, HTTP adapter).
test: test-fast

test-fast:
	go test ./... -count=1 -skip TestBlackBoxAcceptance

# Full suite, including the independent black-box acceptance verifier
# (real binary, real HTTP/SQLite, real process restart).
test-all:
	go test ./... -count=1

acceptance:
	go run ./cmd/acceptance -workdir results/accept-work

reproduce:
	bash scripts/reproduce.sh

clean:
	rm -rf results/accept-work
