"""Figures for the Technical Architecture.

    ta-01-architecture   the layered system and what sits at its edges
    ta-02-chokepoints    the five concerns that pass through one module each
    ta-03-schema         the three scopes of the database, and the boundary
    ta-04-llm            the path of one LLM call
    ta-11-egress         the path of one outbound HTTP request

Colour rule for this group.  The five chokepoints take the five spectrum hues
in the order the Technical Architecture lists them - SQL, HTTP, LLM, sources,
jobs - and keep those hues wherever they appear, so the LLM figure is the same
teal as the LLM column of the chokepoint figure, and the egress figure the same
blue as the HTTP column.  Anything that is not one of the five is slate, the
system hue.

The schema figure is the one exception: there the groups are journey phases
rather than chokepoints, so private data is violet (Profile), the knowledge
base teal (Discover) and contacts amber (Apply), exactly as on screen.

Every number comes from facts.py.
"""

from __future__ import annotations

import textwrap

import facts
import style

# --- the group's colour assignment ------------------------------------------

SQL_H, SQL_S = style.PHASE[1], style.PHASE_SOFT[1]
HTTP_H, HTTP_S = style.PHASE[2], style.PHASE_SOFT[2]
LLM_H, LLM_S = style.PHASE[3], style.PHASE_SOFT[3]
SRC_H, SRC_S = style.PHASE[4], style.PHASE_SOFT[4]
JOB_H, JOB_S = style.PHASE[5], style.PHASE_SOFT[5]
SYS_H, SYS_S = style.PHASE[0], style.PHASE_SOFT[0]

TITLE_PT = 10.5
SUB_PT = 8.0
BODY_PT = 8.5
SMALL_PT = 7.8
TINY_PT = 7.4


# --- local primitives -------------------------------------------------------


def canvas(height: float):
    """A 6.5-inch canvas whose y units are the same size as its x units."""
    fig, ax = style.figure(width=style.WIDTH, height=height)
    top = 100.0 * height / style.WIDTH
    ax.set_ylim(0, top)
    # Fill the figure, so one unit is 6.5/100 inch and the type sizes chosen
    # here are the type sizes the reader gets at final width.
    ax.set_position([0.0, 0.0, 1.0, 1.0])
    return fig, ax, top


def panel(ax, x, y, w, h, *, edge, fill=None, lw=1.1, radius=1.3, zorder=2):
    from matplotlib.patches import FancyBboxPatch

    p = FancyBboxPatch(
        (x, y), w, h,
        boxstyle=f"round,pad=0,rounding_size={radius}",
        linewidth=lw, edgecolor=edge,
        facecolor=fill if fill is not None else style.SURFACE,
        zorder=zorder,
    )
    ax.add_patch(p)
    return p


def stack(ax, cx, cy, lines, *, leading=3.1, ha="center", zorder=4, family=None):
    """Centre a short stack of (text, size, weight, colour) lines on ``cy``."""
    y = cy + leading * (len(lines) - 1) / 2
    for text, size, weight, colour in lines:
        t = ax.text(cx, y, text, ha=ha, va="center", fontsize=size, weight=weight,
                    color=colour, zorder=zorder)
        if family:
            t.set_fontfamily(family)
        y -= leading


def heading(ax, top, title, subtitle_lines=()):
    ax.text(50, top - 2.6, title, ha="center", va="center",
            fontsize=TITLE_PT, weight="bold", color=style.INK)
    y = top - 6.4
    for line in subtitle_lines:
        ax.text(50, y, line, ha="center", va="center", fontsize=SUB_PT,
                color=style.INK_3)
        y -= 3.2


def footnote(ax, y, text):
    ax.text(50, y, text, ha="center", va="center", fontsize=TINY_PT,
            color=style.INK_3, style="italic")


def band(ax, x, y, w, h, name, details, *, colour, fill=None, name_pt=BODY_PT,
         detail_pt=SMALL_PT):
    """A full-width layer: bold name, then one or two quieter lines."""
    panel(ax, x, y, w, h, edge=colour, fill=fill)
    lines = [(name, name_pt, "bold", colour)]
    lines += [(d, detail_pt, "normal", style.INK_2) for d in details]
    stack(ax, x + w / 2, y + h / 2, lines)


# ---------------------------------------------------------------------------
# ta-01  the shape of the system
# ---------------------------------------------------------------------------


def architecture():
    fig, ax, top = canvas(5.4)
    c = facts.CODEBASE

    heading(
        ax, top, "The shape of the system",
        [f"{c['api_routes']} routes over {c['db_tables']} tables · "
         f"{c['backend_files']} Python modules, {c['frontend_files']} JS modules · "
         f"{c['tests_passing']} tests"],
    )

    band(ax, 0, 68, 100, 6.5, "React SPA",
         [f"{facts.BUNDLE['lazy_chunks']} lazy routes · "
          f"{facts.BUNDLE['after_initial_kb']:.0f} KB initial "
          f"({facts.BUNDLE['after_initial_gzip_kb']:.0f} KB gzip) · "
          "no UI kit, no state manager"],
         colour=SYS_H)

    style.arrow(ax, 50, 68, 50, 65.3, colour=style.INK_3, lw=1.0)
    style.label(ax, 52, 66.6, "/api  ·  session cookie, bound to the client",
                size=TINY_PT, ha="left")

    band(ax, 0, 58.5, 100, 6.5, "FastAPI",
         [f"{facts.API_ROUTERS} routers · {c['api_routes']} routes · "
          "current_seeker is the isolation boundary"],
         colour=SYS_H)

    band(ax, 0, 48.5, 100, 8, "Pipeline",
         ["intake · enrichment · planning · collection · profiling · financial",
          "opportunities · scoring · contacts · generation · post-application"],
         colour=SYS_H)

    services = [
        ("LLMClient", ["routing", "budget · redaction", "fencing · audit"], LLM_H, LLM_S),
        ("EgressClient", ["robots · pacing", "cache", "raw capture"], HTTP_H, HTTP_S),
        ("SourceAdapter", [f"{sum(facts.ADAPTERS.values())} adapters", "plan · fetch",
                           "parse · normalise"], SRC_H, SRC_S),
        ("JobRunner", ["resumable", "pause · cancel", "checkpoints"], JOB_H, JOB_S),
        ("crypto", ["AES-256-GCM", "one key per", "job seeker"], SYS_H, SYS_S),
    ]
    sw = (100 - 4 * 2) / 5
    for i, (name, lines, hue, soft) in enumerate(services):
        x = i * (sw + 2)
        panel(ax, x, 31, sw, 15.5, edge=hue, fill=soft)
        stack(ax, x + sw / 2, 38.7,
              [(name, BODY_PT, "bold", hue)]
              + [(t, TINY_PT, "normal", style.INK_2) for t in lines],
              leading=3.0)

    band(ax, 0, 23, 100, 6.5, "Repositories",
         ["the only place SQL is written"], colour=SQL_H, fill=SQL_S)

    panel(ax, 0, 15, 49, 6.5, edge=SYS_H, fill=style.SURFACE_2)
    stack(ax, 24.5, 18.25, [
        (f"SQLite (WAL) · {c['db_tables']} tables · {c['db_indexes']} indexes",
         BODY_PT, "bold", style.INK),
        ("private per job seeker · shared knowledge base", TINY_PT, "normal", style.INK_2),
    ], leading=3.2)

    panel(ax, 51, 15, 49, 6.5, edge=SYS_H, fill=style.SURFACE_2)
    stack(ax, 75.5, 18.25, [
        ("Filesystem", BODY_PT, "bold", style.INK),
        ("raw documents · generated CVs and PDFs · uploads · exports",
         TINY_PT, "normal", style.INK_2),
    ], leading=3.2)

    ax.plot([0, 100], [11.7, 11.7], color=style.LINE, lw=0.9, ls=(0, (3, 3)), zorder=1)
    ax.text(50, 11.7, "outside this machine", ha="center", va="center",
            fontsize=TINY_PT, color=style.INK_3, style="italic", zorder=5,
            bbox=dict(boxstyle="round,pad=0.25", facecolor=style.SURFACE,
                      edgecolor="none"))

    outside = [
        ("DeepSeek API", ["or a local model", "through LLMClient"], LLM_H),
        ("Job boards · ATS", ["company sites", "through EgressClient"], HTTP_H),
        ("Registries", ["NBB · KBO/BCE · KvK", "through EgressClient"], SRC_H),
        ("Gmail · Resend", ["OAuth, scoped tokens", "through the mail layer"], SYS_H),
    ]
    ow = (100 - 3 * 2) / 4
    for i, (name, lines, hue) in enumerate(outside):
        x = i * (ow + 2)
        panel(ax, x, 1.5, ow, 8, edge=hue, fill=style.SURFACE, lw=1.0)
        stack(ax, x + ow / 2, 5.5,
              [(name, SMALL_PT, "bold", hue)]
              + [(t, TINY_PT, "normal", style.INK_3) for t in lines],
              leading=2.9)
        style.arrow(ax, x + ow / 2, 13.6, x + ow / 2, 9.9,
                    colour=style.INK_3, lw=0.9, dashed=True)

    return style.save(fig, "ta-01-architecture")


# ---------------------------------------------------------------------------
# ta-02  the five chokepoints
# ---------------------------------------------------------------------------


CHOKEPOINTS = [
    ("Every SQL\nstatement", ["db/repositories/*", "db/connection.py"],
     [("CR-408", "portable to Postgres"), ("FR-344", "no cross-seeker read")],
     SQL_H, SQL_S),
    ("Every outbound\nHTTP request", ["egress/client.py"],
     [("FR-182", "robots and pacing"), ("FR-183", "every response kept"),
      ("IR-102", "one retry policy")],
     HTTP_H, HTTP_S),
    ("Every LLM\ncall", ["llm/client.py"],
     [("NFR-104", "bounded spend"), ("NFR-205", "injection defence"),
      ("CR-410", "redaction on exit"), ("FR-362", "model routing"),
      ("FR-364", "every call audited")],
     LLM_H, LLM_S),
    ("Every external\nsource", ["adapters/base.py"],
     [("NFR-601", "one file per source"), ("IR-101", "terms acknowledged")],
     SRC_H, SRC_S),
    ("Every long-running\ntask", ["jobs/runner.py"],
     [("FR-185", "pause, cancel, skip"), ("NFR-401", "crash-resumable")],
     JOB_H, JOB_S),
]


def chokepoints():
    fig, ax, top = canvas(5.0)
    c = facts.CODEBASE

    heading(
        ax, top, "The five chokepoints",
        ["Each concern passes through exactly one module, which is what makes the",
         "requirement below it a property of that module rather than a convention."],
    )

    band(ax, 0, 60, 100, 5.6, "The whole application",
         [f"{c['backend_files']} Python modules · {c['api_routes']} routes · "
          f"{sum(facts.ADAPTERS.values())} adapters"],
         colour=SYS_H, fill=SYS_S, detail_pt=TINY_PT)

    cw = (100 - 4 * 2) / 5
    band_lo, band_hi = 4.5, 40.5
    for i, (concern, modules, reqs, hue, soft) in enumerate(CHOKEPOINTS):
        x = i * (cw + 2)
        cx = x + cw / 2

        style.arrow(ax, cx, 60, cx, 58.0, colour=style.INK_3, lw=0.9)

        panel(ax, x, band_lo - 1.0, cw, 58.6 - band_lo, edge=soft, fill=soft,
              lw=0.8, zorder=1)
        panel(ax, x, 50, cw, 7.6, edge=hue, fill=hue, lw=0, zorder=2)
        stack(ax, cx, 53.8,
              [(line, BODY_PT, "bold", style.SURFACE) for line in concern.split("\n")],
              leading=3.4)

        panel(ax, x, 42.5, cw, 6.0, edge=hue, fill=style.SURFACE, lw=0.9, zorder=2)
        stack(ax, cx, 45.5, [(m, TINY_PT, "normal", style.INK) for m in modules],
              leading=2.9, family="monospace")

        # requirement chips, as a block centred in the space below
        wrapped = [(rid, textwrap.wrap(phrase, 20)) for rid, phrase in reqs]
        blocks = [2.9 + 2.6 * (len(lines) - 1) for _, lines in wrapped]
        total = sum(blocks) + 4.0 * (len(blocks) - 1)
        y = (band_lo + band_hi) / 2 + total / 2
        for (rid, lines), bh in zip(wrapped, blocks, strict=True):
            ax.text(cx, y, rid, ha="center", va="center", fontsize=SMALL_PT,
                    weight="bold", color=hue, zorder=4)
            for j, part in enumerate(lines):
                ax.text(cx, y - 2.9 - j * 2.6, part, ha="center", va="center",
                        fontsize=TINY_PT, color=style.INK_2, zorder=4)
            y -= bh + 4.0

    footnote(ax, 1.6, "Requirement identifiers as cited in the code that enforces them.")
    return style.save(fig, "ta-02-chokepoints")


# ---------------------------------------------------------------------------
# ta-03  the three scopes of the schema
# ---------------------------------------------------------------------------


def schema():
    fig, ax, top = canvas(4.6)
    s = facts.SCHEMA_SCOPES
    p = facts.SCHEMA_PRINCIPAL

    heading(
        ax, top, "One database, three scopes",
        [f"{facts.CODEBASE['db_tables']} tables: {s['private']} carry a job_seeker_id, "
         f"{s['shared']} carry none, {s['ledger']} records the migrations."],
    )

    # the boundary, drawn first so the contact panel can sit across it
    ax.plot([50, 50], [2.8, 58], color=style.INK_3, lw=1.1, ls=(0, (4, 3)), zorder=1)

    def scope(x, w, title, tag, tables, rest, hue, soft):
        panel(ax, x, 22.5, w, 35.5, edge=hue, fill=soft, lw=1.2)
        ax.text(x + w / 2, 55, title, ha="center", va="center", fontsize=BODY_PT,
                weight="bold", color=hue, zorder=4)
        ax.text(x + w / 2, 51.6, tag, ha="center", va="center", fontsize=TINY_PT,
                color=style.INK_2, zorder=4)
        rows = (len(tables) + 1) // 2
        for i, name in enumerate(tables):
            col, row = divmod(i, rows)
            ax.text(x + 2.4 + col * (w - 4.8) / 2, 46.5 - row * 3.15, name,
                    ha="left", va="center", fontsize=TINY_PT,
                    color=style.INK, family="monospace", zorder=4)
        ax.text(x + w / 2, 25.0, f"+ {rest} more", ha="center",
                va="center", fontsize=TINY_PT, color=style.INK_3, style="italic",
                zorder=4)

    scope(0, 44, "PRIVATE", "every query filters on the job seeker",
          p["private"], s["private"] - len(p["private"]),
          style.PHASE[1], style.PHASE_SOFT[1])
    scope(56, 44, "SHARED", "the knowledge base, with no link back",
          p["shared"], s["shared"] - len(p["shared"]),
          style.PHASE[3], style.PHASE_SOFT[3])

    ax.text(50, 41, "FR-344", ha="center", va="center", fontsize=SMALL_PT,
            weight="bold", color=style.INK_2, zorder=5,
            bbox=dict(boxstyle="round,pad=0.32", facecolor=style.SURFACE,
                      edgecolor=style.LINE, linewidth=0.9))

    # contact: the one table that straddles the boundary
    hue, soft = style.PHASE[4], style.PHASE_SOFT[4]
    panel(ax, 8, 4.0, 84, 16.5, edge=hue, fill=soft, lw=1.2, zorder=3)
    ax.text(50, 17.6, "RESTRICTED  ·  contact", ha="center", va="center",
            fontsize=BODY_PT, weight="bold", color=hue, zorder=5)
    ax.text(29, 14.4, "collected by browser automation", ha="center", va="center",
            fontsize=TINY_PT, weight="bold", color=style.INK, zorder=5)
    stack(ax, 29, 8.8, [
        ("shareable = 0, owning_campaign_id set,", TINY_PT, "normal", style.INK_2),
        ("retention_until applies, and no other", TINY_PT, "normal", style.INK_2),
        ("job seeker ever sees the row  (NFR-303)", TINY_PT, "normal", style.INK_2),
    ], leading=2.9, zorder=5)
    ax.text(71, 14.4, "collected over HTTP", ha="center", va="center",
            fontsize=TINY_PT, weight="bold", color=style.INK, zorder=5)
    stack(ax, 71, 8.8, [
        ("shared with every job seeker on the", TINY_PT, "normal", style.INK_2),
        ("installation, and an objection blocks", TINY_PT, "normal", style.INK_2),
        ("the address for all of them  (NFR-302)", TINY_PT, "normal", style.INK_2),
    ], leading=2.9, zorder=5)

    footnote(ax, 1.8,
             "FR-344 is the invariant: no shared row carries a job-seeker id, so "
             "erasing a job seeker leaves the market data standing.")
    return style.save(fig, "ta-03-schema")


# ---------------------------------------------------------------------------
# ta-04  the path of one LLM call
# ---------------------------------------------------------------------------


LLM_STEPS = [
    ("Redact", ["do-not-disclose fields and special-category",
                "data stripped before anything leaves"], ["CR-410", "FR-106 · FR-127"]),
    ("Route", ["administrator setting, then a per-task model,",
               "then the cheap / strong split"], ["FR-362"]),
    ("Route · privacy", ["composite profile, tailored CV and motivation",
                         "to a local endpoint when one is configured"], ["NFR-306"]),
    ("Fence", ["scraped text goes in labelled untrusted blocks,",
               "never into the instruction"], ["NFR-205"]),
    ("Check the budget", ["pre-flight estimate; shed optional work",
                          "rather than stop mid-campaign"], ["NFR-104"]),
    ("Call the provider", ["OpenAI-compatible POST to DeepSeek,",
                           "or to the local model"], ["CR-409"]),
    ("Validate", ["the response is parsed and checked",
                  "before any of it is used"], ["NFR-205"]),
    ("Debit and audit", ["tokens, cost, latency, model and prompt version",
                         "written to llm_call"], ["NFR-104", "FR-364"]),
]


def llm_call():
    fig, ax, top = canvas(5.4)

    heading(
        ax, top, "One LLM call, end to end",
        ["Every call in the system takes this path, because llm/client.py is the only",
         "module that speaks to a model."],
    )

    panel(ax, 0, 63.5, 100, 8.5, edge=LLM_H, fill=LLM_S, lw=1.1)
    stack(ax, 50, 67.75, [
        ('llm.complete_json("extract.vacancy", system=…, user=…,',
         SMALL_PT, "normal", style.INK),
        ('                  untrusted={"page": html}, entity_type="vacancy")',
         SMALL_PT, "normal", style.INK),
    ], leading=3.2, family="monospace")
    style.label(ax, 50, 61.2,
                "the task id is what routing, cost attribution and the audit row all key off",
                size=TINY_PT)

    row_h, gap = 5.8, 0.9
    y = 58.4
    spine_x = 4.6
    first_cy = last_cy = None
    for i, (name, detail, ids) in enumerate(LLM_STEPS, start=1):
        cy = y - row_h / 2
        first_cy = cy if first_cy is None else first_cy
        last_cy = cy
        panel(ax, 1.6, y - row_h, 98.4, row_h, edge=style.LINE, lw=0.9)
        ax.plot([spine_x], [cy], marker="o", markersize=11.5, color=LLM_H, zorder=5)
        ax.text(spine_x, cy, str(i), ha="center", va="center", fontsize=TINY_PT,
                weight="bold", color=style.SURFACE, zorder=6)
        ax.text(9.5, cy, name, ha="left", va="center", fontsize=BODY_PT,
                weight="bold", color=style.INK, zorder=4)
        stack(ax, 33.5, cy, [(d, SMALL_PT, "normal", style.INK_2) for d in detail],
              leading=2.7, ha="left")
        stack(ax, 98.4, cy, [(t, TINY_PT, "bold", LLM_H) for t in ids],
              leading=2.7, ha="right")
        y -= row_h + gap

    ax.plot([spine_x, spine_x], [last_cy, first_cy], color=LLM_H, lw=1.4, zorder=4)

    footnote(ax, 1.8,
             f"{facts.CODEBASE['prompt_templates']} versioned prompt templates; the "
             "version travels into llm_call, so a bad output can be tied to the "
             "revision that produced it.")
    return style.save(fig, "ta-04-llm")


# ---------------------------------------------------------------------------
# ta-11  the path of one outbound request
# ---------------------------------------------------------------------------


EGRESS_STEPS = [
    ("Cache", [("look up the URL", style.INK_2), ("hash; TTL bound", style.INK_2)]),
    ("robots.txt", [("parsed per domain", style.INK_2),
                    ("disallow raises", style.DANGER)]),
    ("Pace", [("per-domain limit,", style.INK_2), ("semaphore-bounded", style.INK_2)]),
    ("Fetch", [("httpx, async,", style.INK_2), ("retried", style.INK_2)]),
    ("Capture", [("body to disk under", style.INK_2), ("its content hash", style.INK_2)]),
    ("Parse", [("JSON-LD, selectors,", style.INK_2), ("then the LLM", style.INK_2)]),
]


def egress():
    fig, ax, top = canvas(4.0)

    heading(
        ax, top, "One outbound request, end to end",
        ["No other module in the system opens an HTTP connection."],
    )

    sw = (100 - 5 * 2.2) / 6
    xs = [i * (sw + 2.2) for i in range(6)]
    row_y, row_h = 27.0, 12.0

    for i, (name, lines) in enumerate(EGRESS_STEPS):
        x = xs[i]
        panel(ax, x, row_y, sw, row_h, edge=HTTP_H, fill=HTTP_S, lw=1.1)
        stack(ax, x + sw / 2, row_y + row_h / 2,
              [(name, BODY_PT, "bold", HTTP_H)]
              + [(t, TINY_PT, "normal", col) for t, col in lines], leading=2.9)
        if i:
            style.arrow(ax, xs[i - 1] + sw, row_y + row_h / 2, x - 0.4,
                        row_y + row_h / 2, colour=style.INK_3, lw=1.0)

    # a cache hit never reaches the network
    style.arrow(ax, xs[0] + sw / 2, row_y + row_h, xs[5] + sw / 2, row_y + row_h,
                colour=HTTP_H, lw=1.0, connection="arc3,rad=-0.20")
    ax.text(50, 47.2, "cache hit — nothing leaves the machine", ha="center",
            va="center", fontsize=TINY_PT, color=HTTP_H, zorder=5,
            bbox=dict(boxstyle="round,pad=0.28", facecolor=style.SURFACE,
                      edgecolor="none"))

    # 429 / 503 backs off and retries
    style.arrow(ax, xs[3] + sw / 2, row_y, xs[2] + sw / 2, row_y,
                colour=style.WARN, lw=1.0, connection="arc3,rad=-0.5")
    style.label(ax, (xs[2] + xs[3] + sw) / 2, 20.2,
                "429 / 503 — exponential back-off", size=TINY_PT, colour=style.WARN)

    stores = [
        (0, "http_cache", ["body path, etag,", "expires_at"]),
        (1, "robots_cache", ["one parsed rule", "set per domain"]),
        (4, "raw_document", ["data/raw/<hash>", "url, status, hash"]),
    ]
    store_h, store_y = 11.0, 4.5
    for idx, name, detail in stores:
        x, cx = xs[idx], xs[idx] + sw / 2
        panel(ax, x, store_y, sw, store_h, edge=style.LINE, fill=style.SURFACE_2, lw=1.0)
        stack(ax, cx, store_y + store_h / 2 + 2.4,
              [(name, SMALL_PT, "bold", style.INK)], family="monospace")
        stack(ax, cx, store_y + store_h / 2 - 1.7,
              [(t, TINY_PT, "normal", style.INK_3) for t in detail], leading=2.7)
        style.arrow(ax, cx, row_y, cx, store_y + store_h + 0.4, colour=style.INK_3,
                    lw=0.9, dashed=True, style="<|-|>")

    footnote(ax, 0.8,
             "FR-183 · every response is kept under its content hash, so an extractor "
             "can be re-run on the page it originally saw.")
    return style.save(fig, "ta-11-egress")


def main():
    for path in (architecture(), chokepoints(), schema(), llm_call(), egress()):
        print(path)


if __name__ == "__main__":
    main()
