"""Public errors identify the failed stage without requiring message parsing."""


class AnyError(Exception):
    """Base error for a failed workflow; no partial task output is intentional."""


class ContractError(AnyError):
    """An input, recipe, parameter, or output violates its declared contract."""


class PermissionError(AnyError):
    """The invocation did not authorize the requested filesystem effect."""


class GenerationError(AnyError):
    """Recipe generation failed; no alternate source or automatic repair was used."""


class SandboxError(AnyError):
    """The required operating-system boundary could not be established."""


class ExecutionError(AnyError):
    """A confined program failed or exceeded a resource limit."""

    def __init__(self, message: str, *, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


class CacheError(AnyError):
    """Private recipe storage failed validation or could not be updated."""
