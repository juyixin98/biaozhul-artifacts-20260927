.PHONY: build test race vet fmt demo clean seed

build:
	go build -o bin/routed ./cmd/routed

test:
	go test ./...

race:
	go test -race ./...

vet:
	go vet ./...

fmt:
	gofmt -l -w .

demo: build
	./scripts/demo.sh

seed: build
	mkdir -p data
	RIB_SQLITE_DSN='file:data/rib.db?cache=shared' ./bin/routed -seed configs/seed.example.json

clean:
	rm -rf bin data
