"""Framework errors. Domain validation errors are raised by Problem methods."""


class YggdrisilError(Exception):
    """Base class for Yggdrisil runtime errors."""


class GraphError(YggdrisilError):
    """Errors arising from graph lookup or mutation."""


class UnknownStateError(GraphError):
    def __init__(self, state_id: str) -> None:
        super().__init__(f"unknown state: {state_id}")
        self.state_id = state_id


class CycleError(GraphError):
    """Raised when an edge would introduce a cycle into the DAG."""


class SerializationError(YggdrisilError):
    """Raised when a state, action, or cache value cannot be encoded."""
