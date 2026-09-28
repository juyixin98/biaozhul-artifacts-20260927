.PHONY: build test test-race cover vet fmt run tidy clean

# Pure-Go build (no cgo required thanks to modernc.org/sqlite).
build:
	go build ./...

test:
	go test ./...

test-race:
	go test -race ./...

cover:
	go test -coverprofile=/tmp/cidrcov.cover ./...
	go tool cover -func=/tmp/cidrcov.cover | tail -1

vet:
	go vet ./...

fmt:
	gofmt -l -w .

run:
	go run ./cmd/cidrcovd -config configs/service.json

tidy:
	go mod tidy

clean:
	rm -f data/cidrcov.db data/cidrcov.db-wal data/cidrcov.db-shm
