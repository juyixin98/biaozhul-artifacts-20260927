package admission

// Default values for pipeline configuration. They are deliberately modest:
// missing configuration must not silently disable timeouts or allow an
// unbounded chain.
const (
	DefaultTimeoutMillis     = 250
	DefaultMaxMutationPasses = 3
)

func withDefaults(c Config) Config {
	if c.DefaultTimeoutMS <= 0 {
		c.DefaultTimeoutMS = DefaultTimeoutMillis
	}
	if c.MaxMutationPasses <= 0 {
		c.MaxMutationPasses = DefaultMaxMutationPasses
	}
	for i := range c.Mutators {
		if c.Mutators[i].FailurePolicy == "" {
			c.Mutators[i].FailurePolicy = FailClose
		}
	}
	for i := range c.Defaults {
		if c.Defaults[i].FailurePolicy == "" {
			c.Defaults[i].FailurePolicy = FailClose
		}
	}
	for i := range c.Validators {
		if c.Validators[i].FailurePolicy == "" {
			c.Validators[i].FailurePolicy = FailClose
		}
	}
	return c
}
