"""The lxml-backed selectolax compatibility layer (crawler/sweep parsing).

The pages that killed the API worker were malformed: unclosed tags, mismatched
end tags, truncated markup.  myhtml aborted the process on them; these tests pin
the replacement's behaviour on exactly that shape of input, including the
selectors, text and attribute semantics the hot paths rely on.
"""

from __future__ import annotations

from dreamjob.pipeline import html_dom

MALFORMED = (
    "<html><head><title>Acme &amp; Co</title>"
    "<meta name='description' content='Builders of things'>"
    "<script>var broken = '</p>';</script></head>"
    "<body><nav>Home About</nav>"
    "<main><h1>Acme <em>builds</em> software</h1>"
    "<p class='lede'>We <b>ship <i>fast</i></p></b>"
    "<div id='links'><a class='result__a' href='/about'>About <span>us</span></a>"
    "<a class='result__a' href='/jobs'>Jobs"
    "<table><tr><td class='benaming'>Acme BV</td><td>0473.191.041</td></tr>"
    "<tr><td class='benaming'>Acme NV<tr><td>Other"
)


def test_css_selects_tags_classes_ids_and_lists() -> None:
    tree = html_dom.Html(MALFORMED)
    assert [node.text(strip=True) for node in tree.css("td.benaming")] == ["Acme BV", "Acme NV"]
    assert tree.css_first(".lede") is not None
    assert tree.css_first("#links") is not None
    assert len(tree.css("a.result__a")) == 2
    assert len(tree.css("script, style")) == 1


def test_css_matches_attribute_selectors() -> None:
    tree = html_dom.Html(MALFORMED)
    meta = tree.css_first('meta[name="description"]')
    assert meta is not None
    assert meta.attributes["content"] == "Builders of things"
    assert meta.attr("content") == "Builders of things"
    assert tree.css_first("meta[property='og:image']") is None
    truncated = html_dom.Html('<script type="application/ld+json">{"a": 1}')
    assert truncated.css_first('script[type="application/ld+json"]') is not None


def test_text_joins_chunks_and_strips() -> None:
    node = html_dom.Html(MALFORMED).css_first("h1")
    assert node is not None
    assert node.text() == "Acme builds software"
    assert node.text(strip=True) == "Acmebuildssoftware"
    assert node.text(separator=" ", strip=True) == "Acme builds software"
    assert node.text(separator="|") == "Acme |builds| software"
    assert node.text(deep=False) == "Acme  software"


def test_body_text_includes_text_without_raising() -> None:
    tree = html_dom.Html(MALFORMED)
    body = tree.body
    assert body is not None
    text = body.text(separator=" ", strip=True)
    assert "Acme builds software" in text
    assert "Jobs" in text


def test_decompose_and_strip_tags_remove_nodes() -> None:
    tree = html_dom.Html(MALFORMED)
    for node in tree.css("script"):
        node.decompose()
    for node in tree.css("nav"):
        node.decompose()
    text = tree.body.text(strip=True) if tree.body else ""
    assert "var broken" not in text
    assert "Home About" not in text

    stripped = html_dom.Html(MALFORMED)
    stripped.strip_tags(["script", "style", "nav"])
    text = stripped.body.text(strip=True) if stripped.body else ""
    assert "var broken" not in text
    assert "Home About" not in text


def test_malformed_and_empty_input_never_raises() -> None:
    for raw in (
        "",
        "   ",
        "plain text with no markup",
        "<div class='x'><span>trunc",
        "<p><b>bold</p></b>",
        "</p></div></td>stray end tags",
        "<a href='/x' class=",
        "<html><body><script>while(1){</script>",
        b"<p>bytes</p>",
    ):
        tree = html_dom.Html(raw)
        assert isinstance(tree.css("a, p, div, body"), list)
        assert isinstance(tree.text(), str)


def test_unknown_selector_returns_empty_without_raising() -> None:
    tree = html_dom.Html(MALFORMED)
    assert tree.css("main > section:nth-child(2)") == []
    assert tree.css_first("main > section:nth-child(2)") is None
    assert tree.css_first("") is None


def test_attributes_are_plain_strings() -> None:
    tree = html_dom.Html(MALFORMED)
    link = tree.css_first("a.result__a")
    assert link is not None
    assert link.attributes == {"class": "result__a", "href": "/about"}
    assert link.attr("missing") is None
    assert link.attr("href") == "/about"
    assert link.tag == "a"


def test_parity_expectations_on_crashing_shapes() -> None:
    """Values the selectolax versions produced, asserted directly."""
    tree = html_dom.Html("<p>a</p><p>b")
    assert [node.text() for node in tree.css("p")] == ["a", "b"]
    assert tree.body is not None
    assert tree.body.text(separator="\n") == "a\nb"

    nested = html_dom.Html("<div class='result'><a class='result__a' href='/u'>T <b>U</b></a></div>")
    link = nested.css_first("a.result__a")
    assert link is not None
    assert link.text(strip=True) == "TU"
    assert link.text(separator=" ", strip=True) == "T U"
    assert link.attributes["href"] == "/u"

    assert html_dom.Html("<p>caf&eacute;</p>").css_first("p").text() == "café"
