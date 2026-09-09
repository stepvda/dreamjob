"""Handle derivation from declared URLs must never mangle the account name.

The profile stores its links exactly as the LinkedIn export and CV carry them -
scheme-less in many places ("github.com/stepvda", "linkedin.com/in/stepvda").
``_handles_from_url`` builds the identity anchors that the online enrichment
(FR-122) uses to probe likely profile URLs, so a bad handle means the run
fetches a dozen non-existent addresses and comes back empty.

Two failure modes are locked in here:

1. A scheme-less URL was normalised for ``urlparse`` but not for
   ``registrable_domain``, so ``github.com/stepvda`` resolved to a domain of
   ``github.com/stepvda`` - neither a shared platform nor a clean 2-label
   domain. The fallback branch then returned the domain label ``github``
   instead of the account ``stepvda``.

2. Account-in-path platforms (LinkedIn, X, GitHub) were treated as if the
   account were the domain's own label. ``linkedin.com/in/stepvda`` yielded
   ``linkedin``, and every search probed ``stepvda.substack.com`` against the
   name-derived ``stephaneaa`` guess rather than the real handle.
"""

from __future__ import annotations

import pytest
from dreamjob.pipeline.enrichment import candidate_urls
from dreamjob.pipeline.identity_match import (
    _handles_from_url,
    build_anchors,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # Scheme-less, account in the path - the common case in an export.
        ("github.com/stepvda", "stepvda"),
        ("linkedin.com/in/stepvda", "stepvda"),
        ("www.linkedin.com/in/stepvda", "stepvda"),
        ("gitlab.com/stepvda", "stepvda"),
        ("x.com/stepvda", "stepvda"),
        ("twitter.com/stepvda", "stepvda"),
        # Account in the subdomain.
        ("stepvda.substack.com", "stepvda"),
        # Own domain: the registrable label, not a subdomain.
        ("one.witysk.org", "witysk"),
        ("stepvda.net", "stepvda"),
    ],
)
def test_handle_from_an_account_path_is_the_account_not_the_domain(url, expected):
    assert _handles_from_url(url)[0] == expected


def test_the_domain_label_is_never_returned_as_a_handle():
    """``github``, ``linkedin`` and ``twitter`` are platform labels, not accounts."""
    for url in ("github.com/stepvda", "linkedin.com/in/stepvda", "twitter.com/stepvda"):
        handles = _handles_from_url(url)
        assert "github" not in handles
        assert "linkedin" not in handles
        assert "twitter" not in handles


def test_declared_url_handles_win_over_name_guesses():
    """``build_anchors`` promotes the real handle from a declared URL to the front.

    The name-derived guesses (``stephaneaa``, ``stephane.aa``) may remain in the
    tail as a fallback, but the *probe* set - ``real_handles`` - contains only
    the handle used on a declared link.  That is what prevents an enrichment
    run from probing a table of fabricated addresses.
    """
    sections = {
        "contact": {"websites": [{"url": "github.com/stepvda"}, {"url": "linkedin.com/in/stepvda"}]},
    }
    anchors = build_anchors(sections, display_name="Stephane van der Aa")
    assert anchors.handles[0] == "stepvda"
    # The real handle is tracked separately, so the run probes it and nothing
    # fabricated.
    assert "stepvda" in anchors.real_handles
    assert "linkedin" not in anchors.real_handles
    assert not (anchors.real_handles & {"stephaneaa", "stephane.aa"})


def test_candidate_urls_use_the_real_handle():
    """The URLs the enrichment run fetches are built from the real handle."""
    sections = {
        "contact": {"websites": [{"url": "github.com/stepvda"}, {"url": "stepvda.substack.com"}]},
    }
    anchors = build_anchors(sections, display_name="Stephane van der Aa")
    urls = candidate_urls(anchors)
    assert "https://github.com/stepvda" in urls
    assert "https://stepvda.substack.com" in urls
    # The mangled variety that this function used to produce is gone.
    assert "https://github.com/stephaneaa" not in urls


def test_profile_version_without_a_name_keeps_declared_handles():
    """``anchors_for_seeker`` must append name guesses, not replace real ones.

    This is the exact path the live profile exercises: profile_version carries
    no display_name, so the account name falls back and a naive ``handles =
    named.handles`` would discard ``stepvda`` in favour of ``stephaneaa``.
    """
    from unittest.mock import patch

    from dreamjob.pipeline.enrichment import anchors_for_seeker

    profile = {
        "display_name": "",
        "photo_path": None,
        "sections": {"contact": {"websites": [{"url": "github.com/stepvda"}]}},
    }
    # The profile says nothing about the name, so the seeker account name is
    # used; the declared URL must still win.
    with patch(
        "dreamjob.db.repositories.enrichment.latest_profile_version",
        return_value=profile,
    ), patch(
        "dreamjob.db.repositories.enrichment.seeker_display_name",
        return_value="Stephane van der Aa",
    ):
        anchors = anchors_for_seeker("seeker_1")
    assert anchors.handles[0] == "stepvda"
    assert "github" not in anchors.handles
