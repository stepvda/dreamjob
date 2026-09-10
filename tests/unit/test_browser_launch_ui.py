"""The launch command must be readable in full, not just copyable (FR-201).

A person read the command out of the panel, ran what they could see, and got a
Chromium that refused the debugging port: the visible text ended inside
``--user-data-d``, so the browser started with no dedicated profile directory,
opened an ordinary window, and the screen said "No browser attached" with
nothing connecting the two.  The Copy button had the whole string all along -
this only ever bit someone who read or selected it.

Two properties are pinned here, and both are checked without a browser: the
command *data* never splits the argument that failure hinged on, and the CSS
that renders it cannot hide a character at any width.  ``pre-wrap`` plus
``overflow-wrap: anywhere`` means the box never grows past the width it is
given, and none of the rules between the text and the card takes that away.

The chain is checked only as far as the card on purpose.  Above it the shell
does clip - ``.main`` is ``overflow: hidden`` and ``.content`` scrolls - but
that clipping is the page's, and it can only reach a box that overflows its
own width.  Keeping the box inside its width is therefore the whole property,
and it is what the rules below assert.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from dreamjob.browser import session as session_mod

REPO_ROOT = Path(__file__).resolve().parents[2]
CSS = REPO_ROOT / "frontend" / "src" / "styles" / "app.css"
PAGE = REPO_ROOT / "frontend" / "src" / "pages" / "BrowserPage.jsx"

#: Every way a block can hide part of its own text.
CLIPPING = (
    "overflow-x: auto",
    "overflow-x: scroll",
    "overflow: hidden",
    "overflow: auto",
    "text-overflow",
    "white-space: pre;",
    "white-space: nowrap",
    "max-height",
    "-webkit-line-clamp",
)


def rule(selector: str) -> str | None:
    """The declarations of one CSS rule, or None when it is not written."""
    match = re.search(
        rf"(?:^|\}}|\*/)\s*{re.escape(selector)}\s*\{{([^}}]*)\}}", CSS.read_text(), re.M
    )
    return match.group(1) if match else None


def test_the_command_block_wraps_rather_than_scrolling() -> None:
    declarations = rule(".cmd code")
    assert declarations is not None, ".cmd code must be styled"
    assert "white-space: pre-wrap" in declarations
    assert "overflow-wrap: anywhere" in declarations
    for clip in CLIPPING:
        assert clip not in declarations, f".cmd code must not {clip}"


@pytest.mark.parametrize(
    "selector",
    [".cmd", ".cmd-profile", ".col", ".help-steps li", ".help-steps", ".card"],
)
def test_no_ancestor_of_the_command_clips_it(selector: str) -> None:
    """A wrapping ``code`` proves nothing if a rule above it hides the box.

    These five are every rule between the command text and the card that holds
    it; the shell above the card is deliberately excluded - see the module
    docstring.
    """
    declarations = rule(selector)
    if declarations is None:
        pytest.skip(f"{selector} carries no rule of its own")
    for clip in CLIPPING:
        assert clip not in declarations, f"{selector} must not {clip}"


def test_the_page_renders_the_command_whole_and_as_one_text_node() -> None:
    """No slicing in the markup, and no elements between the characters.

    A soft wrap is not a newline, so a hand-selection of the block still yields
    the original single line - but only while the command is one text node.
    Marking up the arguments would put that at the mercy of how each engine
    serialises a selection, which is the one thing that must not be a guess.
    """
    markup = PAGE.read_text()
    blocks = re.findall(r'<div className="cmd(?: cmd-profile)?">(.*?)</div>', markup, re.S)
    assert len(blocks) == 2, "one box for the profile directory, one for the command"
    shown = set()
    for block in blocks:
        code = re.search(r"<code>(.*?)</code>", block, re.S)
        assert code is not None, "every .cmd box shows a <code>"
        shown.add(code.group(1).strip())
    # Bare expressions: nothing sliced, nothing elided, no markup between the
    # characters of the command itself.
    assert shown == {"{c.command}", "{data.profile_dir}"}


def test_the_argument_the_failure_hinged_on_is_never_split(tmp_path) -> None:
    """Whatever the profile path is, it travels as one contiguous argument."""
    profile = tmp_path / ("p" * 200)
    for recipe in session_mod.RECIPES:
        command = recipe.command(9222, profile)
        assert "\n" not in command, f"{recipe.key} must be one line"
        if recipe.family == "firefox":
            assert f'--profile "{profile}"' in command
        else:
            assert f'--user-data-dir="{profile}"' in command
            assert "--remote-debugging-port=9222" in command
