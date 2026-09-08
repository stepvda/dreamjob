"""Introduction paths through the job seeker's network (FR-302, FR-461, CR-405).

A warm introduction outperforms a cold e-mail, so FR-302 asks for the routes
into a target company to be found and offered *as an alternative* to writing to
the hiring contact directly, and FR-461 asks for them to be ranked and for the
message to the **intermediary** to be written.

That last distinction is the one this module exists for.  Two different
messages are involved and they have different readers:

* the introduction e-mail to the hiring contact - a stranger, a formal pitch,
  an unsubscribe sentence (NFR-302, CR-403).  The generation slice owns it.
* the message to the intermediary - somebody the job seeker already knows, a
  small and specific ask, easy to refuse.  :func:`generate_message` writes it.

Five route types are recognised, which is the FR-461 list:

``first_degree``       a connection who already works at the target company
``former_colleague``   a connection who shares an employer with the job seeker
                       and now works at the target company
``alumni_employer``    a connection who worked at one of the job seeker's
                       employers, at any time
``alumni_school``      a connection from the same school
``community``          a shared group, association or community
``second_degree``      a connection who is not at the company but has a named
                       mutual contact there (FR-302's "second-degree via a
                       named mutual contact")

The ranking is ``strength`` - how likely the person is to help - against
``relevance`` - how close they are to the actual hiring decision.  Both are
computed from stored facts only; nothing about the relationship is invented
(CR-405).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from dreamjob.db.repositories import contacts as repo
from dreamjob.llm.client import BudgetExhausted, LLMClient, LLMError
from dreamjob.pipeline import contacts as contact_pipeline
from dreamjob.pipeline import email_patterns as patterns
from dreamjob.pipeline.enrichment import default_llm, load_prompt

log = logging.getLogger(__name__)

PROMPT_NAME = "introduction_request"

FIRST_DEGREE = "first_degree"
FORMER_COLLEAGUE = "former_colleague"
ALUMNI_EMPLOYER = "alumni_employer"
ALUMNI_SCHOOL = "alumni_school"
COMMUNITY = "community"
SECOND_DEGREE = "second_degree"

#: FR-461 ranks routes "by strength and relevance".  This is the strength half:
#: how likely the relationship is to produce an introduction at all.
RELATIONSHIP_STRENGTH: dict[str, float] = {
    # A shared work history is a stronger tie than a bare connection: the two
    # people have actually worked together, which is what an introduction rests
    # on.  A connection at the company still beats an alumnus who is not there.
    FORMER_COLLEAGUE: 0.92,
    FIRST_DEGREE: 0.85,
    ALUMNI_EMPLOYER: 0.70,
    ALUMNI_SCHOOL: 0.60,
    COMMUNITY: 0.50,
    SECOND_DEGREE: 0.45,
}

#: How each relationship reads in the ranked list.  Kept as fixed sentences so
#: the model is *given* the relationship and never asked to characterise it
#: (CR-405); every placeholder is filled from stored facts.
RELATIONSHIP_SENTENCE: dict[str, str] = {
    FIRST_DEGREE: "a direct connection in the job seeker's own network",
    FORMER_COLLEAGUE: "a former colleague at {shared}",
    ALUMNI_EMPLOYER: "someone who also worked at {shared}",
    ALUMNI_SCHOOL: "a fellow alumnus of {shared}",
    COMMUNITY: "a fellow member of {shared}",
    SECOND_DEGREE: "a connection who knows {mutual} at the company",
}

#: The same relationship spelled out for the prompt, with no ambiguity about
#: *which* organisation is shared.  A first live run produced "as a former
#: colleague at Acme Data BV" - the target company, not the shared employer -
#: because the ranked-list phrasing put two company names in one clause; a
#: fabricated shared history is exactly what CR-405 forbids.
RELATIONSHIP_EXPLANATION: dict[str, str] = {
    FIRST_DEGREE: (
        "{name} is a direct connection of {seeker}. {name} works at {company}{role}."
    ),
    FORMER_COLLEAGUE: (
        "{seeker} and {name} were colleagues at {shared}. {name} now works at "
        "{company}{role}. Nothing else about their time together is known - do not "
        "describe any shared project."
    ),
    ALUMNI_EMPLOYER: (
        "{name} also worked at {shared}, one of {seeker}'s former employers, though "
        "not necessarily at the same time. {name} now works at {member_company}{role}."
    ),
    ALUMNI_SCHOOL: (
        "{name} studied at {shared}, the same school {seeker} attended, though not "
        "necessarily in the same year. {name} works at {member_company}{role}."
    ),
    COMMUNITY: (
        "{seeker} and {name} are both members of {shared}. {name} works at "
        "{member_company}{role}."
    ),
    SECOND_DEGREE: (
        "{name} does not work at {company}, but knows {mutual} there. {name} works "
        "at {member_company}{role}."
    ),
}

#: A senior intermediary can introduce; a peer in the right team is closer to
#: the decision.  Relevance blends both.
SENIORITY_TOKENS = (
    "chief", "ceo", "cto", "cfo", "coo", "founder", "owner", "director", "directeur",
    "head of", "vp", "vice president", "managing", "partner", "principal", "manager",
    "lead", "hoofd", "responsable",
)

_WORD_RE = re.compile(r"[a-z0-9&+]+")
_LEGAL_FORM_RE = re.compile(
    r"\b(nv|sa|bv|bvba|sprl|srl|gmbh|ltd|limited|plc|inc|llc|vzw|asbl)\b"
)
_FILLER_RE = re.compile(
    r"\b(the|group|holding|international|belgium|belgie|europe|company)\b"
)
_FORMERLY_RE = re.compile(r"\(\s*(?:formerly|voorheen|ex-?)\s*([^)]+)\)", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")

#: The marker in ``introduction_request.md`` that separates the instruction
#: from the third-party facts, which are fenced as data (NFR-205).
DATA_MARKER = "<<<DATA>>>"


def _plain(value: object, limit: int = 160) -> str:
    """One line of third-party text, safe to place inside a sentence (NFR-205).

    Names, role titles and company names are scraped or browser-collected, so
    they reach the prompt with their line breaks removed and their length
    capped; a paragraph smuggled into a role title cannot become a paragraph of
    instructions.  The fenced data block below covers the rest.
    """
    return " ".join(_CONTROL_RE.sub(" ", str(value or "")).split())[:limit].strip()


def _norm(text: str | None) -> str:
    """A comparable form of an organisation or school name."""
    value = patterns.strip_accents(text or "").lower()
    value = _LEGAL_FORM_RE.sub(" ", value)
    value = _FILLER_RE.sub(" ", value)
    return " ".join(_WORD_RE.findall(value))


def _variants(name: str | None) -> set[str]:
    if not name:
        return set()
    out = {_norm(name)}
    for match in _FORMERLY_RE.finditer(name):
        out.add(_norm(match.group(1)))
    return {v for v in out if v}


# ---------------------------------------------------------------------------
# The job seeker's own history, which is the alumni join key (FR-461)
# ---------------------------------------------------------------------------


@dataclass
class SeekerBackground:
    """Employers, schools and communities read off the stored profile."""

    name: str = ""
    headline: str = ""
    employers: set[str] = field(default_factory=set)
    schools: set[str] = field(default_factory=set)
    communities: set[str] = field(default_factory=set)
    employer_labels: dict[str, str] = field(default_factory=dict)
    school_labels: dict[str, str] = field(default_factory=dict)


def seeker_background(job_seeker_id: str) -> SeekerBackground:
    """Read the alumni and former-colleague join keys out of the profile (FR-461)."""
    sections = repo.latest_profile_sections(job_seeker_id)
    background = SeekerBackground(name=repo.display_name(job_seeker_id))

    experience = sections.get("experience") or []
    if isinstance(experience, list) and experience:
        first = experience[0]
        if isinstance(first, dict):
            title = first.get("title") or ""
            company = first.get("company") or ""
            background.headline = " at ".join(x for x in (title, company) if x)
    for entry in experience if isinstance(experience, list) else []:
        if not isinstance(entry, dict):
            continue
        for variant in _variants(entry.get("company")):
            background.employers.add(variant)
            background.employer_labels.setdefault(variant, entry.get("company") or variant)

    for entry in sections.get("education") or []:
        if not isinstance(entry, dict):
            continue
        for variant in _variants(entry.get("school")):
            background.schools.add(variant)
            background.school_labels.setdefault(variant, entry.get("school") or variant)

    for key in ("organizations", "organisations", "volunteering", "groups", "communities"):
        for entry in sections.get(key) or []:
            label = entry.get("name") if isinstance(entry, dict) else entry
            for variant in _variants(str(label) if label else None):
                background.communities.add(variant)
    return background


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@dataclass
class IntroductionRoute:
    """One ranked way into the company through a person (FR-302, FR-461)."""

    member_id: str | None
    name: str
    role: str | None
    company_name: str | None
    linkedin_url: str | None
    relationship: str
    degree: int
    strength: float
    relevance: float
    rationale: str
    explanation: str = ""
    shared: str | None = None
    mutual_name: str | None = None
    target_contact_id: str | None = None
    target_name: str | None = None
    target_role: str | None = None
    message_subject: str | None = None
    message_draft: str | None = None
    path_id: str | None = None
    status: str = "proposed"

    @property
    def rank_score(self) -> float:
        """FR-461: strength and relevance, weighted towards actually helping."""
        return round(0.6 * self.strength + 0.4 * self.relevance, 4)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path_id": self.path_id,
            "network_member_id": self.member_id,
            "intermediary_name": self.name,
            "intermediary_role": self.role,
            "intermediary_linkedin": self.linkedin_url,
            "company_name": self.company_name,
            "relationship": self.relationship,
            "degree": self.degree,
            "strength": round(self.strength, 3),
            "relevance": round(self.relevance, 3),
            "score": self.rank_score,
            "shared": self.shared,
            "mutual_name": self.mutual_name,
            "target_contact_id": self.target_contact_id,
            "target_name": self.target_name,
            "target_role": self.target_role,
            "message_subject": self.message_subject,
            "message_draft": self.message_draft,
            "status": self.status,
            "rationale": self.rationale,
        }

    def as_row(self, opportunity_id: str, company_id: str | None) -> dict[str, Any]:
        return {
            "opportunity_id": opportunity_id,
            "company_id": company_id,
            "target_contact_id": self.target_contact_id,
            "network_member_id": self.member_id,
            "intermediary_name": self.name,
            "intermediary_role": self.role,
            "intermediary_linkedin": self.linkedin_url,
            "relationship": self.relationship,
            "degree": self.degree,
            "strength": round(self.strength, 3),
            "relevance": round(self.relevance, 3),
            "rationale": self.rationale[:1000],
            "message_subject": self.message_subject,
            "message_draft": self.message_draft,
            "status": self.status,
        }


def _shared_label(
    relationship: str, member: dict, background: SeekerBackground
) -> str | None:
    """The organisation the relationship rests on, recomputed from stored facts.

    Used when an :class:`IntroductionRoute` is rebuilt from a stored path: the
    row keeps the relationship type but not what was shared, and a message that
    names the wrong organisation is exactly the fabricated history CR-405
    forbids.
    """
    if relationship in (FORMER_COLLEAGUE, ALUMNI_EMPLOYER):
        keys = {v for e in member.get("employers") or [] for v in _variants(str(e))}
        common = sorted(keys & background.employers)
        return background.employer_labels.get(common[0], common[0]) if common else None
    if relationship == ALUMNI_SCHOOL:
        keys = {v for s in member.get("schools") or [] for v in _variants(str(s))}
        common = sorted(keys & background.schools)
        return background.school_labels.get(common[0], common[0]) if common else None
    if relationship == COMMUNITY:
        keys = {v for c in member.get("communities") or [] for v in _variants(str(c))}
        common = sorted(keys & background.communities)
        return common[0] if common else None
    return None


def explanation_for(
    relationship: str,
    member: dict,
    background: SeekerBackground,
    *,
    company_name: str | None = None,
    shared: str | None = None,
) -> str:
    """The relationship, spelled out for the prompt with no ambiguity (CR-405).

    The target company and the intermediary's own employer go in different
    clauses on purpose; a single clause carrying both is what produced an
    invented shared history on the first live run.
    """
    template = RELATIONSHIP_EXPLANATION.get(relationship) or RELATIONSHIP_EXPLANATION[FIRST_DEGREE]
    if shared is None:
        shared = _shared_label(relationship, member, background)
    role = _plain(member.get("role_title") or member.get("headline"), 80)
    return template.format(
        name=_plain(member.get("full_name")) or "they",
        seeker=background.name or "the job seeker",
        shared=_plain(shared) or "a shared organisation",
        mutual=_plain(member.get("mutual_name")) or "a shared contact",
        company=_plain(company_name or member.get("company_name")) or "the company",
        member_company=_plain(member.get("company_name")) or "their current employer",
        role=f" as {role}" if role else "",
    )


def refresh_explanation(
    job_seeker_id: str, route: IntroductionRoute, *, company_name: str | None = None
) -> str:
    """Rebuild the relationship sentence for a route restored from the database.

    ``introduction_path`` stores the ranked-list rationale, which names the
    shared organisation and the target company in one clause; handing that to
    the model as the relationship is what made it write a shared project that
    never happened (CR-405).  The stored network member is re-read instead, and
    when it is gone the sentence degrades to the relationship type alone rather
    than to the ambiguous rationale.
    """
    background = seeker_background(job_seeker_id)
    member = (
        repo.get_network_member(route.member_id, job_seeker_id) if route.member_id else None
    ) or {
        "full_name": route.name,
        "role_title": route.role,
        "company_name": None,
        "mutual_name": route.mutual_name,
    }
    return explanation_for(
        route.relationship,
        member,
        background,
        company_name=company_name or route.company_name,
        shared=route.shared,
    )


def _seniority(role: str | None) -> float:
    title = patterns.strip_accents(role or "").lower()
    if not title:
        return 0.3
    return 0.8 if any(token in title for token in SENIORITY_TOKENS) else 0.45


def _function_overlap(role: str | None, wanted: set[str]) -> float:
    if not role or not wanted:
        return 0.0
    tokens = set(_WORD_RE.findall(patterns.strip_accents(role).lower()))
    overlap = tokens & wanted
    return round(min(1.0, len(overlap) / 3.0), 3) if overlap else 0.0


def classify_relationship(
    member: dict, background: SeekerBackground, *, at_company: bool
) -> tuple[str, str | None]:
    """Which FR-461 route type this person is, and what is shared."""
    employers = {v for e in member.get("employers") or [] for v in _variants(str(e))}
    schools = {v for s in member.get("schools") or [] for v in _variants(str(s))}
    communities = {v for c in member.get("communities") or [] for v in _variants(str(c))}

    shared_employer = sorted(employers & background.employers)
    shared_school = sorted(schools & background.schools)
    shared_community = sorted(communities & background.communities)

    employer_label = (
        background.employer_labels.get(shared_employer[0], shared_employer[0])
        if shared_employer
        else None
    )
    school_label = (
        background.school_labels.get(shared_school[0], shared_school[0])
        if shared_school
        else None
    )
    shared = employer_label or school_label or (shared_community[0] if shared_community else None)

    if at_company:
        if shared_employer:
            return FORMER_COLLEAGUE, employer_label
        if int(member.get("degree") or 1) == 1:
            return FIRST_DEGREE, None
        return SECOND_DEGREE, shared

    # Off-company: what makes this person a route into *this* company is the
    # mutual contact they can name (FR-302); the shared history is why they
    # would agree to use it, and is carried alongside.
    if member.get("mutual_name") or int(member.get("degree") or 1) >= 2:
        return SECOND_DEGREE, shared
    if shared_employer:
        return ALUMNI_EMPLOYER, employer_label
    if shared_school:
        return ALUMNI_SCHOOL, school_label
    if shared_community:
        return COMMUNITY, shared_community[0]
    return COMMUNITY, None


def build_routes(
    job_seeker_id: str,
    opportunity_id: str,
    *,
    limit: int = 10,
    target: dict[str, Any] | None = None,
) -> list[IntroductionRoute]:
    """Rank the introduction routes into one opportunity's company (FR-302, FR-461).

    ``target`` is the hiring contact the introduction should lead to; when it
    is not given, the best usable contact for the company is used (FR-301), so
    the route and the cold e-mail aim at the same person.
    """
    context = repo.opportunity_context(opportunity_id, job_seeker_id)
    if context is None:
        raise LookupError(f"No opportunity {opportunity_id} for this job seeker")

    company_id = context.get("company_id")
    company_name = context.get("company_name")
    background = seeker_background(job_seeker_id)
    wanted = contact_pipeline.function_tokens(
        context.get("function_family"), context.get("title")
    )

    if target is None and company_id:
        target = contact_pipeline.best_contact(company_id, campaign_id=context.get("campaign_id"))
    target = target or {}

    at_company = repo.network_at_company(
        job_seeker_id, company_id=company_id, company_name=company_name
    )
    at_company_ids = {m["id"] for m in at_company}
    elsewhere = [m for m in repo.list_network(job_seeker_id) if m["id"] not in at_company_ids]

    routes: list[IntroductionRoute] = []
    for member in at_company:
        routes.append(
            _route_for(
                member, background, wanted,
                at_company=True, target=target, company_name=company_name,
            )
        )
    for member in elsewhere:
        # Off-company people are only a route when they can actually name
        # somebody at the company, or share a history worth citing (FR-461).
        relationship, shared = classify_relationship(member, background, at_company=False)
        if relationship == SECOND_DEGREE and not member.get("mutual_name"):
            continue
        if relationship in (COMMUNITY,) and not shared:
            continue
        routes.append(
            _route_for(
                member, background, wanted,
                at_company=False, target=target, company_name=company_name,
            )
        )

    routes.sort(key=lambda r: -r.rank_score)
    return routes[:limit]


def _route_for(
    member: dict,
    background: SeekerBackground,
    wanted: set[str],
    *,
    at_company: bool,
    target: dict[str, Any],
    company_name: str | None = None,
) -> IntroductionRoute:
    relationship, shared = classify_relationship(member, background, at_company=at_company)
    strength = RELATIONSHIP_STRENGTH.get(relationship, 0.4)
    if member.get("last_interaction_at"):
        strength = min(1.0, strength + 0.05)
    if int(member.get("degree") or 1) > 1:
        strength *= 0.8

    role = member.get("role_title") or member.get("headline")
    relevance = 0.0
    if at_company:
        relevance = round(
            min(1.0, 0.45 + 0.35 * _seniority(role) + 0.4 * _function_overlap(role, wanted)), 3
        )
    else:
        relevance = round(0.2 + 0.2 * _seniority(role), 3)

    sentence = RELATIONSHIP_SENTENCE[relationship].format(
        shared=shared or "a shared organisation",
        mutual=member.get("mutual_name") or "a shared contact",
    )
    if relationship == SECOND_DEGREE and shared:
        sentence += f", and shares a history at {shared}"

    where = f" at {member.get('company_name')}" if member.get("company_name") else ""
    rationale = f"{member['full_name']} is {sentence}{where}."
    if at_company:
        rationale += " They work at the target company."

    explanation = explanation_for(
        relationship, member, background, company_name=company_name, shared=shared
    )

    return IntroductionRoute(
        member_id=member.get("id"),
        name=member["full_name"],
        role=role,
        company_name=member.get("company_name"),
        linkedin_url=member.get("linkedin_url"),
        relationship=relationship,
        degree=int(member.get("degree") or 1),
        strength=round(strength, 3),
        relevance=relevance,
        rationale=rationale,
        explanation=explanation,
        shared=shared,
        mutual_name=member.get("mutual_name"),
        target_contact_id=target.get("contact_id"),
        target_name=target.get("full_name"),
        target_role=target.get("role_title"),
    )


# ---------------------------------------------------------------------------
# The message to the intermediary (FR-461)
# ---------------------------------------------------------------------------


def fallback_message(
    route: IntroductionRoute,
    *,
    seeker_name: str,
    company_name: str | None,
    opportunity_title: str | None,
    language: str = "en",
) -> tuple[str, str]:
    """The message written without an LLM (NFR-104 degradation, CR-405).

    Deliberately plain: it states the relationship, the ask and the way out,
    using only stored facts.  A job seeker can send it as it stands.
    """
    target = route.target_name or "the person responsible for hiring"
    role = f" ({route.target_role})" if route.target_role else ""
    company = company_name or route.company_name or "the company"
    opening = opportunity_title or "a role that fits my background"
    shared = f" from {route.shared}" if route.shared else ""

    subject = f"A quick question about {company}"
    body = (
        f"Hi {route.name.split(' ')[0]},\n\n"
        f"I hope you are well. I am reaching out because we know each other{shared}, "
        f"and I am currently looking at {opening} at {company}.\n\n"
        f"Would you be willing to introduce me to {target}{role}? A single sentence "
        f"putting us in touch would be plenty - I will take it from there and will not "
        f"take up more of your time.\n\n"
        f"If this is awkward for any reason, or you would rather not, please just say so "
        f"and I will leave it at that - no explanation needed.\n\n"
        f"Thanks either way,\n{seeker_name}"
    )
    if language.lower().startswith("nl"):
        subject = f"Korte vraag over {company}"
        body = (
            f"Dag {route.name.split(' ')[0]},\n\n"
            f"Ik contacteer je omdat we elkaar kennen{shared} en ik momenteel kijk naar "
            f"{opening} bij {company}.\n\n"
            f"Zou je me willen voorstellen aan {target}{role}? Een enkele zin volstaat; "
            f"daarna neem ik het zelf over.\n\n"
            f"Voel je vrij om nee te zeggen - dan laat ik het hierbij, zonder verdere uitleg.\n\n"
            f"Alvast bedankt,\n{seeker_name}"
        )
    return subject, body


def generate_message(
    job_seeker_id: str,
    route: IntroductionRoute,
    *,
    company_name: str | None,
    opportunity_title: str | None,
    why_company: str = "",
    seeker_goal: str = "",
    language: str = "en",
    campaign_id: str | None = None,
    llm: LLMClient | None = None,
) -> tuple[str, str]:
    """Write the message asking the intermediary for an introduction (FR-461).

    The reader is somebody the job seeker knows, so the prompt is given the
    relationship as a fixed sentence and told to invent nothing (CR-405).  With
    no provider configured, or with the campaign's budget spent, the
    deterministic message is returned instead of failing the run (NFR-104).
    """
    background = seeker_background(job_seeker_id)
    seeker_name = background.name or "the job seeker"
    if not route.explanation:
        route.explanation = refresh_explanation(
            job_seeker_id, route, company_name=company_name
        )
    client = llm if llm is not None else default_llm(job_seeker_id, campaign_id)
    if client is None:
        return fallback_message(
            route,
            seeker_name=seeker_name,
            company_name=company_name,
            opportunity_title=opportunity_title,
            language=language,
        )

    template = load_prompt(PROMPT_NAME)
    system, user = template.render(
        language=language,
        seeker_name=seeker_name,
        seeker_headline=background.headline or "not stated",
        seeker_goal=seeker_goal or "a role in line with their current experience",
        intermediary_name=_plain(route.name) or "not stated",
        intermediary_role=_plain(route.role, 80) or "not stated",
        relationship_explanation=route.explanation,
        company_name=_plain(company_name or route.company_name) or "the company",
        target_name=_plain(route.target_name) or "the hiring manager",
        target_role=_plain(route.target_role, 80) or "not stated",
        opportunity_title=_plain(opportunity_title) or "not stated",
        why_company=why_company or "not stated",
    )
    # NFR-205: the names, roles and company came off public pages, so they are
    # fenced as data instead of being concatenated into the instruction.
    instruction, _, facts = user.partition(DATA_MARKER)
    try:
        payload = client.complete_json(
            template.task,
            system=system,
            user=instruction.strip() or user,
            untrusted={"introduction_facts": facts.strip()} if facts.strip() else None,
            schema_hint='{"subject": str, "body": str}',
            prompt_template=template.name,
            prompt_version=template.version,
            entity_type="introduction_path",
            entity_id=route.path_id,
            # A 120-word note to somebody the job seeker already knows, with
            # the facts fixed by the prompt: the reasoning model spent ten
            # times the tokens deliberating and produced the same message
            # (RK-07).  The chat model is the right instrument here.
            prefer_strong=False,
            max_tokens=900,
            temperature=0.4,
        )
    except (BudgetExhausted, LLMError) as exc:
        log.info("Falling back to the deterministic introduction message: %s", exc)
        return fallback_message(
            route,
            seeker_name=seeker_name,
            company_name=company_name,
            opportunity_title=opportunity_title,
            language=language,
        )

    subject = str((payload or {}).get("subject") or "").strip()
    body = str((payload or {}).get("body") or "").strip()
    if not body:
        return fallback_message(
            route,
            seeker_name=seeker_name,
            company_name=company_name,
            opportunity_title=opportunity_title,
            language=language,
        )
    body = _single_signature(body, seeker_name)
    return subject or f"A quick question about {company_name or route.company_name}", body


def _single_signature(body: str, seeker_name: str) -> str:
    """Leave exactly one sign-off, spelled with the job seeker's full name.

    Models sign off whatever the prompt says, so the application cannot simply
    append its own name: the first live run produced "Best regards, Stephane"
    followed by a second "Stephane van der Aa".
    """
    lines = body.rstrip().splitlines()
    parts = {p for p in patterns.strip_accents(seeker_name).lower().split() if len(p) > 1}
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        tail = patterns.strip_accents(lines[-1]).lower().strip(" ,.-")
        if tail and (set(tail.split()) & parts):
            lines[-1] = seeker_name
            return "\n".join(lines)
    return "\n".join(lines) + f"\n\n{seeker_name}"


# ---------------------------------------------------------------------------
# Persisting and offering the routes (FR-302)
# ---------------------------------------------------------------------------


def propose_paths(
    job_seeker_id: str,
    opportunity_id: str,
    *,
    limit: int = 10,
    write_messages: bool = True,
    use_llm: bool = True,
    language: str | None = None,
    replace: bool = True,
) -> list[IntroductionRoute]:
    """Build, draft and store the introduction routes for one opportunity."""
    context = repo.opportunity_context(opportunity_id, job_seeker_id)
    if context is None:
        raise LookupError(f"No opportunity {opportunity_id} for this job seeker")

    routes = build_routes(job_seeker_id, opportunity_id, limit=limit)
    if replace:
        repo.delete_paths_for_opportunity(job_seeker_id, opportunity_id)

    llm = default_llm(job_seeker_id, context.get("campaign_id")) if use_llm else None
    lang = language or context.get("language") or "en"
    for route in routes:
        if write_messages:
            subject, body = generate_message(
                job_seeker_id,
                route,
                company_name=context.get("company_name"),
                opportunity_title=context.get("title"),
                language=lang,
                campaign_id=context.get("campaign_id"),
                llm=llm,
            )
            route.message_subject, route.message_draft = subject, body
        path_id, _created = repo.upsert_introduction_path(
            job_seeker_id, route.as_row(opportunity_id, context.get("company_id"))
        )
        route.path_id = path_id
    return routes


def stored_paths(job_seeker_id: str, opportunity_id: str) -> list[dict]:
    return repo.introduction_paths(job_seeker_id, opportunity_id=opportunity_id)


def outreach_options(job_seeker_id: str, opportunity_id: str) -> dict[str, Any]:
    """FR-302: offer "request an introduction" beside the cold e-mail.

    The recommendation is advisory only - the job seeker decides (NFR-305).
    """
    context = repo.opportunity_context(opportunity_id, job_seeker_id)
    if context is None:
        raise LookupError(f"No opportunity {opportunity_id} for this job seeker")

    company_id = context.get("company_id")
    contact = (
        contact_pipeline.best_contact(company_id, campaign_id=context.get("campaign_id"))
        if company_id
        else None
    )
    paths = repo.introduction_paths(job_seeker_id, opportunity_id=opportunity_id)
    best_path = paths[0] if paths else None

    cold_available = bool(contact and contact.get("email"))
    intro_available = bool(best_path)
    recommended = "cold_email" if cold_available else None
    if intro_available and float(best_path.get("strength") or 0) >= 0.6:
        recommended = "introduction"
    elif intro_available and not cold_available:
        recommended = "introduction"

    return {
        "opportunity_id": opportunity_id,
        "company_id": company_id,
        "cold_email": {
            "available": cold_available,
            "contact": contact,
            "note": (
                "A direct message to the hiring contact, with the objection sentence "
                "every introduction e-mail carries (NFR-302, CR-403)."
            ),
        },
        "introduction": {
            "available": intro_available,
            "best_path": best_path,
            "path_count": len(paths),
            "note": (
                "A message to somebody in your own network asking them to introduce you "
                "(FR-302). Warmer, slower, and it needs their consent."
            ),
        },
        "recommended": recommended,
    }


# ---------------------------------------------------------------------------
# Importing the network (FR-302)
# ---------------------------------------------------------------------------


def import_members(
    job_seeker_id: str,
    members: list[dict[str, Any]],
    *,
    source: str = "linkedin_export",
    access_method: str = "manual",
    campaign_id: str | None = None,
) -> dict[str, int]:
    """Store people from the job seeker's network (FR-302, NFR-303, RK-08).

    Only the fields the ranking uses are kept - name, headline, employer,
    schools, the mutual contact - and browser-collected members inherit the
    campaign scope and deletion date that NFR-303 requires.
    """
    scope = contact_pipeline.storage_scope(access_method, campaign_id)
    created = updated = skipped = 0
    for raw in members:
        name = str(raw.get("full_name") or raw.get("name") or "").strip()
        if not name:
            skipped += 1
            continue
        payload = {
            "full_name": name,
            "headline": raw.get("headline"),
            "role_title": raw.get("role_title") or raw.get("position"),
            "company_name": raw.get("company_name") or raw.get("company"),
            "company_id": raw.get("company_id"),
            "linkedin_url": raw.get("linkedin_url") or raw.get("url"),
            "degree": int(raw.get("degree") or 1),
            "mutual_name": raw.get("mutual_name"),
            "mutual_linkedin": raw.get("mutual_linkedin"),
            "schools": list(raw.get("schools") or []),
            "employers": list(raw.get("employers") or []),
            "communities": list(raw.get("communities") or []),
            "connected_at": raw.get("connected_at"),
            "last_interaction_at": raw.get("last_interaction_at"),
            "source": source,
            "access_method": access_method,
        }
        if scope.get("owning_campaign_id"):
            payload["owning_campaign_id"] = scope["owning_campaign_id"]
        if scope.get("retention_until"):
            payload["retention_until"] = scope["retention_until"]
        _id, was_created = repo.upsert_network_member(job_seeker_id, payload)
        created += int(was_created)
        updated += int(not was_created)
    return {"created": created, "updated": updated, "skipped": skipped}
