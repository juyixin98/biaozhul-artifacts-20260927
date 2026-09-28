module example.com/cgcoord/coordinator

go 1.23.4

require (
	example.com/cgcoord/kernel v0.0.0
	example.com/cgcoord/protocol v0.0.0
	example.com/cgcoord/replay v0.0.0
	example.com/cgcoord/storage v0.0.0
)

require github.com/lib/pq v1.10.9 // indirect

replace (
	example.com/cgcoord/kernel => ../kernel
	example.com/cgcoord/protocol => ../protocol
	example.com/cgcoord/replay => ../replay
	example.com/cgcoord/storage => ../storage
)
