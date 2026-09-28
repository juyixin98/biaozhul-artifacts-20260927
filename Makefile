.PHONY: build test test-short run fmt vet clean verify examples

# Offline-friendly: the module cache already contains every dependency, so the
# default build/test never needs network access. To force network resolution,
# run with NET=1 (GOPROXY defaults are then used).
ifeq ($(NET),1)
GOFLAGS := -mod=mod
else
GOFLAGS := -mod=mod
GOPROXY := off
export GOPROXY
endif
export GOFLAGS

BIN := bin/cidrsvc

build:
	go build -o $(BIN) ./cmd/cidrsvc

run: build
	mkdir -p data
	./$(BIN) -config configs/config.json

test:
	go test ./... -count=1

# Quick gate without the 65k-case exhaustive sweeps.
test-short:
	go test ./... -count=1 -short

fmt:
	gofmt -w cmd internal *.go

vet:
	go vet ./...

verify: vet test
	@echo "verify: vet + full test suite passed"

clean:
	rm -rf bin data/*.db data/*.db-wal data/*.db-shm
