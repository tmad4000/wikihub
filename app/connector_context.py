"""In-process identity only; never populated from an HTTP header or cookie."""
from contextvars import ContextVar
connector_user = ContextVar("wikihub_connector_user", default=None)
