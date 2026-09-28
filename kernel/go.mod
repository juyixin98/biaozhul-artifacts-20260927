module dsnet/kernel

go 1.23

require (
	dsnet/compute v0.0.0
	dsnet/proto v0.0.0
)

replace (
	dsnet/compute => ../compute
	dsnet/proto => ../proto
)
