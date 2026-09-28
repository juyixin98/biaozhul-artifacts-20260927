package domain

import (
	"fmt"
	"regexp"
)

// Label name rules, simplified from the k8s spec: an optional DNS subdomain
// prefix followed by a qualified name made of word characters, '-' and '.'.
var (
	dnsSubdomainRE = regexp.MustCompile(`^[a-z0-9]([a-z0-9.\-]{0,251}[a-z0-9])?$`)
	namePartRE     = regexp.MustCompile(`^[A-Za-z0-9]([A-Za-z0-9_.\-]{0,61}[A-Za-z0-9])?$`)
)

func validateLabelKey(k string) error {
	if k == "" {
		return fmt.Errorf("label key must not be empty")
	}
	name := k
	if i := lastSlash(k); i >= 0 {
		prefix := k[:i]
		name = k[i+1:]
		if !dnsSubdomainRE.MatchString(prefix) {
			return fmt.Errorf("label key %q has invalid DNS subdomain prefix", k)
		}
	}
	if !namePartRE.MatchString(name) {
		return fmt.Errorf("label key %q has invalid name part", k)
	}
	return nil
}

func validateLabelValue(v string) error {
	if v == "" {
		return nil // empty value is legal
	}
	if len(v) > 63 || !namePartRE.MatchString(v) {
		return fmt.Errorf("label value %q must be at most 63 alphanumeric word characters", v)
	}
	return nil
}

func validateLabels(labels map[string]string) error {
	for k, v := range labels {
		if err := validateLabelKey(k); err != nil {
			return err
		}
		if err := validateLabelValue(v); err != nil {
			return fmt.Errorf("label %q: %w", k, err)
		}
	}
	return nil
}

func lastSlash(s string) int {
	for i := len(s) - 1; i >= 0; i-- {
		if s[i] == '/' {
			return i
		}
	}
	return -1
}
