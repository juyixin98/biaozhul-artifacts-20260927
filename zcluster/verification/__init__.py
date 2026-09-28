"""Built-in verification suite exposed through /api/verify."""

from .checks import CHECKS_VERSION, run_all

__all__ = ["CHECKS_VERSION", "run_all"]
