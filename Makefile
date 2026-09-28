.PHONY: all build test test-race fmt vet run dev clean

all: build

build:
	go build -o bin/controller ./cmd/controller
	go build -o bin/fakeservice ./cmd/fakeservice

test:
	go test ./... -count=1

test-race:
	go test ./... -count=1 -race

fmt:
	gofmt -l -w .

vet:
	go vet ./...

dev:
	./scripts/dev.sh

examples:
	./scripts/examples.sh

clean:
	rm -rf bin data
