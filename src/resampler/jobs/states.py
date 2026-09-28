"""Job lifecycle states and transitions."""

OPEN = "open"          # accepting chunks
FLUSHED = "flushed"    # tail released, complete output available
FAILED = "failed"      # terminal, error_* columns populated

TERMINAL = {FLUSHED, FAILED}
