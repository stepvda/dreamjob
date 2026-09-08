"""Model-call helper for the generators (CR-409, NFR-104).

``generate.cv``, ``generate.email``, ``generate.motivation`` and
``generate.briefing`` are routed to the strong model by ``llm/client.py``, and
for DeepSeek that is the *reasoning* model.  Its reasoning tokens are charged
against the same ``max_tokens`` ceiling as its answer, so a long document
prompt can consume the whole allowance thinking and return an empty message -
a success at the HTTP level and nothing at all at ours.

Rather than paper over that with an ever-larger ceiling, a call that comes back
empty or unparseable is retried once against the chat model.  These are writing
tasks over material that has already been assembled; the chat model does them
well, and a degraded document beats a failed run (NFR-104).
"""

from __future__ import annotations

import logging
from typing import Any

from dreamjob.llm.client import LLMClient, LLMError

log = logging.getLogger(__name__)

#: DeepSeek's ceiling for a single completion.
MAX_OUTPUT_TOKENS = 8000

_DEGRADABLE = ("empty llm response", "could not parse json")


def complete_json(llm: LLMClient, task: str, system: str, user: str, **kwargs: Any) -> Any:
    """``LLMClient.complete_json`` with the reasoning-budget fallback."""
    kwargs.setdefault("max_tokens", MAX_OUTPUT_TOKENS)
    try:
        return llm.complete_json(task, system, user, **kwargs)
    except LLMError as exc:
        message = str(exc).lower()
        if not any(reason in message for reason in _DEGRADABLE):
            raise
        log.info("Task %s returned nothing usable from the strong model; retrying cheap", task)
        kwargs["prefer_strong"] = False
        return llm.complete_json(task, system, user, **kwargs)
