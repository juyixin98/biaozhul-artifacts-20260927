.PHONY: build test race vet smoke clean

build:
	go build -o bin/fieldapply ./cmd/fieldapply

test:
	go test -count=1 ./...

race:
	go test -race -count=1 ./...

vet:
	go vet ./...

run: build
	./bin/fieldapply -addr 127.0.0.1:8080 -db fieldapply.db -journal fieldapply.journal.jsonl

smoke: build
	@./bin/fieldapply -addr 127.0.0.1:8080 -db :memory: -journal - & pid=$$!; \
	sleep 1; BASE=http://localhost:8080 bash examples/smoke.sh; kill $$pid

clean:
	rm -rf bin fieldapply.db fieldapply.db-* fieldapply.journal.jsonl
