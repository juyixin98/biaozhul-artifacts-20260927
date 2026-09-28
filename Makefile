.PHONY: build test test-race test-verbose demo clean fmt vet run

build:
	go build -o bin/rollctl ./cmd/rollctl

test:
	go test -count=1 ./...

test-race:
	go test -race -count=1 ./...

test-verbose:
	go test -count=1 -v ./...

vet:
	go vet ./...

fmt:
	gofmt -l -w .

demo: build
	./scripts/demo.sh all

run: build
	./bin/rollctl -http 127.0.0.1:8080 -db ./data/rollctl.db

clean:
	rm -rf bin data
