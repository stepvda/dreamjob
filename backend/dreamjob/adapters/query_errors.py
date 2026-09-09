"""Typed adapter failures that must never be reported as an empty result set.

A source adapter has two ways of collecting nothing, and the difference is the
whole of NFR-403 and half of FR-181:

* the query ran and the source genuinely had nothing to say - a board with no
  open roles, a register with no deposit for that year;
* the query could not be run at all, because the plan item does not carry the
  key the adapter needs (a company for a register, a site for a crawl, a feed
  for a newsroom).

The second is a failure of the plan, not an answer from the source, and every
adapter in this package raises :class:`UnusableQuery` for it rather than
returning ``[]``.  ``collection_worker`` records a raised exception against the
plan item it came from, so the source shows up as failed with a message that
names the missing key, instead of "done, 0 records, 0 errors".
"""

from __future__ import annotations


class AdapterQueryError(ValueError):
    """Base class for a plan item an adapter cannot execute.

    ``ValueError`` because the fault is in the value the plan item carries, not
    in the run - and because ``dreamjob.adapters.vacancy_source`` raises a
    ``ValueError`` of its own for the same situation on the vacancy side.
    """


class UnusableQuery(AdapterQueryError):
    """The plan item lacks something the adapter cannot work without (FR-181).

    ``expected`` names the native_query keys that would have made the item
    runnable, so the message read off the dashboard says what to fix.
    """

    def __init__(self, adapter_key: str, detail: str, *, expected: tuple[str, ...] = ()):
        self.adapter_key = adapter_key
        self.detail = detail
        self.expected = tuple(expected)
        message = f"[{adapter_key}] {detail}"
        if self.expected:
            message += f"; the query must carry one of: {', '.join(self.expected)}"
        super().__init__(message)
