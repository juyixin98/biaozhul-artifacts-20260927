.PHONY: build test test-race vet fmt run demo clean

build:
	go build -o bin/apiserver ./cmd/apiserver
	go build -o bin/actualserver ./cmd/actualserver
	go build -o bin/controller ./cmd/controller

test:
	go test ./...

test-race:
	go test -race ./...

vet:
	go vet ./...

fmt:
	gofmt -w ./cmd ./internal

demo: build
	python3 examples/demo.py
	./examples/demo-lost-create.sh

clean:
	rm -rf bin data/*.db data/*.db-wal data/*.db-shm logs
