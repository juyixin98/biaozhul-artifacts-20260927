package memstore

import "errors"

var (
	errInjected = errors.New("injected store failure")
	errDup      = errors.New("active port/flow already exists")
	errNotFound = errors.New("mapping not found")
)
