package jsonpatch

import "encoding/json"

// deepCopy duplicates a generic JSON tree via a JSON round-trip.
func deepCopy(v any) (any, error) {
	b, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	var out any
	if err := json.Unmarshal(b, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// DeepCopyObject duplicates a resource object and returns a value independent
// of the input (map/slice headers are not shared).
func DeepCopyObject(src any) (any, error) {
	return deepCopy(src)
}

func jsonUnmarshal(b []byte, v *any) error {
	return json.Unmarshal(b, v)
}

// DocumentOf encodes a typed value (e.g. types.Object) to its generic tree.
func DocumentOf(v any) (map[string]any, error) {
	b, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	var doc map[string]any
	if err := json.Unmarshal(b, &doc); err != nil {
		return nil, err
	}
	return doc, nil
}

// ObjectFrom decodes a generic tree into the typed resource object.
func ObjectFrom(doc any, dst any) error {
	b, err := json.Marshal(doc)
	if err != nil {
		return err
	}
	return json.Unmarshal(b, dst)
}
