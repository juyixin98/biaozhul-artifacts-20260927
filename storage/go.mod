module example.com/cgcoord/storage

go 1.23.4

require (
	example.com/cgcoord/protocol v0.0.0
	github.com/lib/pq v1.10.9
)

replace example.com/cgcoord/protocol => ../protocol
