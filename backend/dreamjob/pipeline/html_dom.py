"""Minimal selectolax-compatible HTML surface backed by lxml.

The crawler and the contact sweep used ``selectolax``, whose myhtml engine is
native code that corrupts its heap on some malformed pages: the process dies
with SIGSEGV/SIGABRT inside ``HTMLParser.__init__`` and takes the whole API
server with it.  ``lxml`` is already a dependency and is written to survive
broken markup, so this module exposes the small slice of the selectolax API the
hot paths actually use - ``css``/``css_first``/``text``/``attributes`` - on top
of it.

Only the selector subset reached by the crawl/sweep code is translated by hand
(tag, ``.class``, ``#id``, ``[attr]``, ``[attr=value]``, ``[attr*=value]`` and
comma-separated lists); anything richer falls back to ``cssselect`` when it is
installed.  A selector that cannot be translated yields no nodes instead of an
exception, and malformed HTML never raises: parsing failures degrade to an
empty document.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

try:  # pragma: no cover - exercised by the environment, not by tests
    from lxml import html as _lxml_html

    LXML_AVAILABLE = True
except Exception:  # noqa: BLE001 - a missing/broken lxml must not break imports
    _lxml_html = None  # type: ignore[assignment]
    LXML_AVAILABLE = False

try:  # pragma: no cover - cssselect is optional and absent here
    from cssselect import GenericTranslator as _CssTranslator

    _TRANSLATOR = _CssTranslator()
except Exception:  # noqa: BLE001
    _TRANSLATOR = None

_WS = re.compile(r"\s+")
_SIMPLE = re.compile(r"^(?P<tag>[A-Za-z_][\w-]*|\*)?(?P<rest>(?:[.#][\w-]+|\[[^\]]*\])*)$")
_CHUNK = re.compile(r"[.#][\w-]+|\[[^\]]*\]")
_ATTR = re.compile(
    r"""^\[\s*(?P<name>[\w:.-]+)\s*"""
    r"""(?:(?P<op>[~|^$*]?=)\s*(?P<value>"[^"]*"|'[^']*'|[^\]\s]+))?\s*\]$"""
)


def _split_list(selector: str) -> list[str] | None:
    """Split a selector list on top-level commas, ignoring brackets and quotes."""
    parts: list[str] = []
    depth = 0
    quote = ""
    start = 0
    for index, char in enumerate(selector):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(selector[start:index])
            start = index + 1
    parts.append(selector[start:])
    return [part.strip() for part in parts if part.strip()] or None


def _attr_xpath(raw: str) -> str | None:
    match = _ATTR.match(raw)
    if not match:
        return None
    name = match.group("name")
    op = match.group("op")
    value = match.group("value")
    if ":" in name:
        attr = f"@*[name()={_quote(name)}]"
    else:
        attr = f"@{name}"
    if op is None:
        return attr
    if value is None:
        return None
    if value[:1] in "\"'" and value[-1:] == value[:1] and len(value) >= 2:
        value = value[1:-1]
    quoted = _quote(value)
    if op == "=":
        return f"{attr} = {quoted}"
    if op == "*=":
        return f"contains({attr}, {quoted})"
    if op == "^=":
        return f"starts-with({attr}, {quoted})"
    if op == "$=":
        return f"substring({attr}, string-length({attr}) - {len(value)} + 1) = {quoted}"
    if op == "~=":
        return f"contains(concat(' ', normalize-space({attr}), ' '), {_quote(' ' + value + ' ')})"
    if op == "|=":
        return f"({attr} = {quoted} or starts-with({attr}, {_quote(value + '-')}))"
    return None


def _quote(value: str) -> str:
    if '"' in value and "'" not in value:
        return f"'{value}'"
    return '"' + value.replace('"', "&quot;") + '"'


def _simple_xpath(selector: str) -> str | None:
    match = _SIMPLE.match(selector)
    if not match:
        return None
    tag = match.group("tag") or "*"
    predicates: list[str] = []
    for chunk in _CHUNK.findall(match.group("rest") or ""):
        if chunk.startswith("."):
            cls = chunk[1:]
            predicates.append(
                "contains(concat(' ', normalize-space(@class), ' '), "
                + _quote(" " + cls + " ")
                + ")"
            )
        elif chunk.startswith("#"):
            predicates.append(f"@id = {_quote(chunk[1:])}")
        else:
            predicate = _attr_xpath(chunk)
            if predicate is None:
                return None
            predicates.append(predicate)
    return ".//" + tag + ("[" + " and ".join(predicates) + "]" if predicates else "")


def _compile(selector: str) -> str | None:
    """Translate a CSS selector to a relative XPath expression, or ``None``."""
    if not selector:
        return None
    parts = _split_list(selector)
    if not parts:
        return None
    translated: list[str] = []
    for part in parts:
        xpath = _simple_xpath(part)
        if xpath is not None:
            translated.append(xpath)
    if len(translated) != len(parts):
        if _TRANSLATOR is None:
            return None
        try:  # pragma: no cover - cssselect is not installed in this venv
            return _TRANSLATOR.css_to_xpath(selector, prefix="descendant-or-self::")
        except Exception:  # noqa: BLE001
            return None
    return " | ".join(translated)


def _is_element(node: Any) -> bool:
    return isinstance(getattr(node, "tag", None), str)


def _iter_text(element: Any, deep: bool) -> Iterator[str]:
    if element.text:
        yield element.text
    for child in element:
        if deep and _is_element(child):
            yield from _iter_text(child, True)
        if child.tail:
            yield child.tail


class Node:
    """One element, with the selectolax methods the hot paths call."""

    __slots__ = ("_element",)

    def __init__(self, element: Any) -> None:
        self._element = element

    def _find(self, selector: str) -> list[Node]:
        xpath = _compile(selector)
        if xpath is None:
            return []
        try:
            found = self._element.xpath(xpath)
        except Exception:  # noqa: BLE001 - malformed trees never raise outward
            return []
        return [Node(element) for element in found if _is_element(element)]

    def css(self, selector: str) -> list[Node]:
        return self._find(selector)

    def css_first(self, selector: str) -> Node | None:
        found = self._find(selector)
        return found[0] if found else None

    @property
    def attributes(self) -> dict[str, str]:
        try:
            return {str(key): str(value) for key, value in self._element.attrib.items()}
        except Exception:  # noqa: BLE001
            return {}

    def attr(self, name: str) -> str | None:
        value = self.attributes.get(name)
        return value

    @property
    def tag(self) -> str:
        tag = getattr(self._element, "tag", "")
        return tag if isinstance(tag, str) else ""

    @property
    def html(self) -> str:
        if not LXML_AVAILABLE:
            return ""
        try:
            return "".join(
                _lxml_html.tostring(child, encoding="unicode") for child in self._element
            )
        except Exception:  # noqa: BLE001
            return ""

    def text(
        self, *, deep: bool = True, separator: str = "", strip: bool = False
    ) -> str:
        try:
            chunks = list(_iter_text(self._element, deep))
        except Exception:  # noqa: BLE001
            return ""
        if strip:
            chunks = [chunk.strip() for chunk in chunks]
            chunks = [chunk for chunk in chunks if chunk]
        return separator.join(chunks)

    def decompose(self) -> None:
        """Detach this element, as selectolax's ``decompose`` does."""
        try:
            parent = self._element.getparent()
            if parent is not None:
                parent.remove(self._element)
        except Exception:  # noqa: BLE001
            return


class Html:
    """A parsed document with the selectolax ``HTMLParser`` subset."""

    def __init__(self, html: str | bytes) -> None:
        self._root_element: Any = None
        self._body_element: Any = None
        if not LXML_AVAILABLE or html is None:
            return
        data = html if isinstance(html, bytes) else str(html)
        if not data.strip():
            return
        try:
            root = _lxml_html.document_fromstring(data)
        except Exception:  # noqa: BLE001 - truncated/broken markup still parses where possible
            try:
                root = _lxml_html.fromstring(data)
            except Exception:  # noqa: BLE001
                return
        self._root_element = root
        try:
            bodies = root.xpath("//body")
        except Exception:  # noqa: BLE001
            bodies = []
        self._body_element = bodies[0] if bodies else root

    def _find(self, selector: str) -> list[Node]:
        if self._root_element is None:
            return []
        return Node(self._root_element)._find(selector)

    def css(self, selector: str) -> list[Node]:
        return self._find(selector)

    def css_first(self, selector: str) -> Node | None:
        found = self._find(selector)
        return found[0] if found else None

    @property
    def root(self) -> Node | None:
        return Node(self._root_element) if self._root_element is not None else None

    @property
    def body(self) -> Node | None:
        return Node(self._body_element) if self._body_element is not None else None

    def text(
        self, *, deep: bool = True, separator: str = "", strip: bool = False
    ) -> str:
        node = self.root if self._root_element is not None else None
        return node.text(deep=deep, separator=separator, strip=strip) if node else ""

    def strip_tags(self, tags: str | list[str] | tuple[str, ...]) -> None:
        """Remove every element named in ``tags`` from the document."""
        if self._root_element is None:
            return
        names = [tags] if isinstance(tags, str) else list(tags)
        for name in names:
            try:
                matches = list(self._root_element.iter(name))
            except Exception:  # noqa: BLE001
                continue
            for element in matches:
                if element is self._root_element:
                    continue
                Node(element).decompose()
