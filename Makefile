.PHONY: build test vet fmt check run clidemo clean

build:
	go build ./...

test:
	go test ./...

vet:
	go vet ./...

fmt:
	gofmt -l -w .

check: vet
	go test -race ./...

run:
	go run ./cmd/server -config configs/config.json

clidemo:
	go run ./cmd/netpolctl matrix \
		--fixture test/fixtures/scenarios/overlapping-selectors.json \
		--port 8080 --protocol TCP

clean:
	rm -rf data *.db *.db-wal *.db-shm
