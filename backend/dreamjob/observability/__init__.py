"""Observability: the logging backbone and the request log (NFR-701, NFR-702).

``logs`` owns the files, the format, the correlation id and the redaction
rule; ``middleware`` writes one line per HTTP request through it.  Everything
else in the code base only needs ``get_logger`` and, for slow work,
``operation``.
"""

from dreamjob.observability.logs import (
    LOG_FILES,
    bind_seeker,
    correlation_scope,
    get_correlation_id,
    get_logger,
    log_path,
    new_correlation_id,
    operation,
    setup_logging,
)
from dreamjob.observability.middleware import RequestLogMiddleware

__all__ = [
    "LOG_FILES",
    "RequestLogMiddleware",
    "bind_seeker",
    "correlation_scope",
    "get_correlation_id",
    "get_logger",
    "log_path",
    "new_correlation_id",
    "operation",
    "setup_logging",
]
