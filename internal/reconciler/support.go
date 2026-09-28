package reconciler

import (
	"reflect"
	"sort"
	"strings"
	"time"

	"infraplanner/internal/model"
	"infraplanner/internal/spec"
)

// specSpec is a thin alias so the signatures read clearly.
type specSpec = spec.Spec

func parseSilent(res []model.Desired) (*spec.Spec, error) {
	p, err := spec.Parse(res)
	if err != nil {
		return nil, model.E(model.CatInput, "stored_spec_invalid",
			"persisted spec failed re-validation: %v", err)
	}
	return p, nil
}

func timeNow() time.Time { return time.Now().UTC() }

// sameLive compares identity-carrying live resources for drift. The physical
// ID, attributes and references must all match. ObservedAt is ignored. Map
// fields are normalized so a nil map equals an empty one (both serialize to
// "no entries"); this matches the canonical form used by Fingerprint.
func sameLive(a, b model.Live) bool {
	if a.Key != b.Key || a.ID != b.ID || a.Protected != b.Protected {
		return false
	}
	if !reflect.DeepEqual(normMap(a.Attrs), normMap(b.Attrs)) {
		return false
	}
	if !reflect.DeepEqual(normRefMap(a.Refs), normRefMap(b.Refs)) {
		return false
	}
	return true
}

func normMap(m map[string]string) map[string]string {
	if m == nil {
		return map[string]string{}
	}
	return m
}

func normRefMap(m map[string]model.Ref) map[string]model.Ref {
	if m == nil {
		return map[string]model.Ref{}
	}
	return m
}

// liveDiff renders the first difference found, for log/error messages.
func liveDiff(a, b model.Live) string {
	var ds []string
	if a.ID != b.ID {
		ds = append(ds, "id "+a.ID+"->"+b.ID)
	}
	if a.Protected != b.Protected {
		ds = append(ds, "protected changed")
	}
	keys := map[string]bool{}
	for k := range a.Attrs {
		keys[k] = true
	}
	for k := range b.Attrs {
		keys[k] = true
	}
	ak := make([]string, 0, len(keys))
	for k := range keys {
		ak = append(ak, k)
	}
	sort.Strings(ak)
	for _, k := range ak {
		if a.Attrs[k] != b.Attrs[k] {
			ds = append(ds, "attr "+k+"="+a.Attrs[k]+"->"+b.Attrs[k])
		}
	}
	rk := map[string]bool{}
	for k := range a.Refs {
		rk[k] = true
	}
	for k := range b.Refs {
		rk[k] = true
	}
	rn := make([]string, 0, len(rk))
	for k := range rk {
		rn = append(rn, k)
	}
	sort.Strings(rn)
	for _, k := range rn {
		av, bv := "", ""
		if r, ok := a.Refs[k]; ok {
			av = r.Key().String()
		}
		if r, ok := b.Refs[k]; ok {
			bv = r.Key().String()
		}
		if av != bv {
			ds = append(ds, "ref "+k+" "+av+"->"+bv)
		}
	}
	return strings.Join(ds, ", ")
}
