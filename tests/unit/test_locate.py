"""FR-144: every opportunity is placed on the map as it is added.

The geocoder is faked throughout: what is under test is which question gets
asked, which answer is believed, and where the opportunity ends up.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from dreamjob.config import get_settings
from dreamjob.db.connection import (
    execute,
    insert_row,
    query_all,
    query_one,
    update_row,
    utcnow,
)
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import opportunities as repo
from dreamjob.pipeline import geocode as geo
from dreamjob.pipeline import locate

BRUSSELS = (50.8467, 4.3525)
RUE_DE_LA_LOI = (50.8462, 4.3687)   # about a kilometre from the centre
MECHELEN_SITE = (51.0300, 4.4800)   # twenty-two kilometres away


def _hit(lat: float, lon: float, rank: int | None, *, cc: str = "BE", kind: str = "city"):
    return geo.GeocodeResult(
        query="q", display_name=kind, latitude=lat, longitude=lon,
        country_code=cc, place_rank=rank, place_type=kind, importance=0.5,
    )


PLACES = {
    "Brussels": _hit(*BRUSSELS, 16),
    "Leuven": _hit(50.8798, 4.7005, 16),
    "Germany": _hit(51.16, 10.45, 4, cc="DE", kind="country"),
    "Belgium": _hit(50.64, 4.67, 4, kind="country"),
    "Rue de la Loi 16, 1000 Brussels": _hit(*RUE_DE_LA_LOI, 30, kind="building"),
    "Kerkstraat 1, Brussels": _hit(50.8500, 4.3400, 30, kind="building"),
    "Industrieweg 5, 2800 Mechelen": _hit(*MECHELEN_SITE, 30, kind="building"),
}


@pytest.fixture()
def geocoder(monkeypatch):
    """A geocoder that knows ``PLACES`` and records every question."""
    asked: list[str] = []

    async def fake(query, *, country_codes=None, egress=None, strict=False, **_):
        asked.append(query)
        return [PLACES[query]] if query in PLACES else []

    class _NoClient:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(geo, "search", fake)
    monkeypatch.setattr(geo, "_geocoder_client", lambda: _NoClient())
    return asked


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings()
    migrate()
    yield
    get_settings.cache_clear()


def _campaign() -> tuple[str, str]:
    seeker = insert_row("job_seeker", {
        "email": "seeker@example.com", "display_name": "Seeker",
        "created_at": utcnow(), "updated_at": utcnow(),
    })
    directives = insert_row("directive_set", {
        "job_seeker_id": seeker, "name": "Brussels", "created_at": utcnow(),
    })
    profile = insert_row("profile_version", {
        "job_seeker_id": seeker, "version": 1, "sections": {}, "created_at": utcnow(),
    })
    campaign = insert_row("campaign", {
        "job_seeker_id": seeker, "directive_set_id": directives,
        "profile_version_id": profile, "name": "Brussels", "created_at": utcnow(),
    })
    return seeker, campaign


def _company(name: str, locations: list[dict], role: str | None = None) -> str:
    company_id = insert_row("company", {
        "name": name, "normalised_name": name.lower(), "country": "BE",
        "locations": locations, "collected_at": utcnow(),
    })
    if role:
        execute(
            "INSERT INTO company_employer_kind (company_id, kind, employer_role, confidence, "
            "rung, method, evidence, established_at) VALUES (?, ?, ?, 0.95, 'signals', "
            "'test', ?, ?)",
            (company_id, role, role, json.dumps([{"text": "test"}]), utcnow()),
        )
    return company_id


def _opportunity(seeker: str, campaign: str, location: str | None, **values) -> str:
    return insert_row("opportunity", {
        "job_seeker_id": seeker, "campaign_id": campaign, "kind": "vacancy",
        "title": "Data Lead", "location": location, "country": "BE",
        "created_at": utcnow(), "updated_at": utcnow(), **values,
    })


def _known(table: dict):
    """``known`` for :func:`locate.decide`, from answers keyed by question text."""
    def known(key: locate.Key):
        if key.text not in table:
            raise locate._Unasked(key)
        hit = table[key.text]
        if hit is None or locate.is_area(hit):
            return None
        return locate.Point(hit.latitude, hit.longitude, hit.place_rank)
    return known


# ---------------------------------------------------------------------------
# What counts as a place
# ---------------------------------------------------------------------------


def test_a_country_or_a_region_is_not_a_place(geocoder):
    assert locate.is_area(PLACES["Germany"])
    assert locate.is_area(_hit(50.9, 4.6, 8, kind="state"))      # Vlaams-Brabant
    assert not locate.is_area(PLACES["Brussels"])
    assert not locate.is_area(_hit(*BRUSSELS, 21, kind="postcode"))
    # A city that is also a state or district is ranked as one, and is still
    # a city: these were all filed as "no such place" by a rank-only test.
    berlin = _hit(52.52, 13.40, 8, cc="DE")
    berlin.bounding_box = [52.34, 52.68, 13.09, 13.76]                 # 59 km
    assert not locate.is_area(berlin)
    assert not locate.is_area(_hit(51.51, -0.13, 10, cc="GB"))        # London
    assert not locate.is_area(_hit(48.14, 11.58, 12, cc="DE"))        # Munich
    assert locate.is_area(_hit(50.9, 4.6, 12, kind="administrative"))  # no box: by rank
    hamburg = _hit(53.55, 10.0, 8, cc="DE")
    hamburg.bounding_box = [53.39, 53.96, 8.10, 10.33]                # islands: 162 km
    assert not locate.is_area(hamburg)
    wuerzburg = _hit(49.79, 9.95, 12, cc="DE", kind="county")
    wuerzburg.bounding_box = [49.71, 49.85, 9.87, 10.01]              # a city-sized county
    assert not locate.is_area(wuerzburg)
    province = _hit(50.9, 4.6, 8, kind="state")
    province.bounding_box = [50.68, 51.08, 4.02, 5.17]                # Vlaams-Brabant
    assert locate.is_area(province)
    # No rank, but a box the size of a country.
    wide = geo.GeocodeResult(query="q", display_name="x", latitude=50.6, longitude=4.6,
                             bounding_box=[49.5, 51.5, 2.5, 6.4])
    assert locate.is_area(wide)

    # "Germany" used to land in the middle of Germany, as if it were a town.
    assert asyncio.run(locate._resolve("Germany", ["DE"], None)) is None


def test_the_town_inside_a_county_of_its_name_is_the_place(monkeypatch):
    county = _hit(48.83, 12.96, 12, cc="DE", kind="county")
    county.bounding_box = [48.62, 49.03, 12.70, 13.30]                # Landkreis Deggendorf
    town = _hit(48.84, 12.96, 16, cc="DE", kind="town")
    country = _hit(51.16, 10.45, 4, cc="DE", kind="country")
    country.bounding_box = [47.27, 55.06, 5.87, 15.04]
    namesake = _hit(51.0, 10.0, 25, cc="DE", kind="hamlet")             # inside, same name

    async def fake(query, **_):
        return [county, town] if query == "Deggendorf" else [country, namesake]

    monkeypatch.setattr(geo, "search", fake)
    assert asyncio.run(locate._first_place("Deggendorf", ["DE"], None)) is town
    # A country is never narrowed to a hamlet that shares its name.
    assert asyncio.run(locate._first_place("Germany", ["DE"], None)) is None


def test_a_three_letter_town_is_a_place_and_a_country_code_is_not():
    assert locate.place_queries("Mol")[0] == ["Mol"]
    assert locate.place_queries("DEU") == ([], [])
    assert locate.place_queries("Brussels (BE)")[0] == ["Brussels"]


def test_employer_sites_read_every_shape_the_sources_write():
    company = {"country": "BE", "locations": [
        {"kind": "seat", "address": "Steenhouwersvest 11"},
        {"kind": "seat", "address": "President Kennedypark(Kor) 35, 8500 Kortrijk",
         "postcode": "8500", "city": "Kortrijk"},
        {"label": "Global headquarters", "city": "New York", "country": "United States",
         "kind": "hq"},
        {"municipality": "Leuven", "postcode": "3000"},
        {"label": "Grabenweg 3a 6020 Innsbruck", "city": "Innsbruck", "country": "AT",
         "kind": "office"},
        {"kind": "office", "country": "US", "address": {
            "street1": "4505 Campus Drive", "street2": None, "city": "College Park",
            "zipcode": "20740"}},
    ]}
    sites = {s.text: s for s in locate.employer_sites(company)}

    assert sites["Steenhouwersvest 11"].needs_town          # which Steenhouwersvest?
    assert sites["Steenhouwersvest 11"].key("Antwerpen").text == "Steenhouwersvest 11, Antwerpen"
    assert not sites["President Kennedypark 35, 8500 Kortrijk"].needs_town
    assert sites["New York"] == locate.Site("New York", "US", street=False, needs_town=False)
    assert sites["3000 Leuven"].country == "BE"
    assert sites["Grabenweg 3a 6020 Innsbruck"].street
    # A source that stored the address as a record, not a line.
    assert sites["4505 Campus Drive, 20740, College Park"].country == "US"


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def _employer(*addresses: str) -> dict:
    return {"country": "BE", "locations": [{"kind": "office", "label": a} for a in addresses]}


def test_the_town_is_enough_and_the_employer_address_is_finer():
    known = _known(PLACES)
    row = {"location": "Brussels", "country": "BE"}

    town = locate.decide(row, None, None, known)
    assert (town.latitude, town.longitude, town.precision) == (*BRUSSELS, locate.LOCALITY)

    office = locate.decide(row, _employer("Rue de la Loi 16, 1000 Brussels"), None, known)
    assert (office.latitude, office.longitude, office.precision) == (
        *RUE_DE_LA_LOI, locate.ADDRESS,
    )

    # A street the register kept without its town is found next to the posting's.
    seat = {"country": "BE", "locations": [{"kind": "seat", "address": "Kerkstraat 1"}]}
    assert locate.decide(row, seat, None, known).precision == locate.ADDRESS


def test_an_agency_address_and_another_office_are_not_the_job():
    known = _known(PLACES)
    row = {"location": "Brussels", "country": "BE"}

    agency = locate.decide(row, _employer("Rue de la Loi 16, 1000 Brussels"), "agency", known)
    assert agency.precision == locate.LOCALITY
    board = locate.decide(row, _employer("Rue de la Loi 16, 1000 Brussels"), "board", known)
    assert board.precision == locate.LOCALITY

    elsewhere = locate.decide(row, _employer("Industrieweg 5, 2800 Mechelen"), None, known)
    assert (elsewhere.latitude, elsewhere.precision) == (BRUSSELS[0], locate.LOCALITY)


def test_without_a_town_only_an_employer_in_one_place_places_the_job():
    known = _known(PLACES)
    bare = {"location": "BE", "country": "BE"}

    single = locate.decide(bare, _employer("Industrieweg 5, 2800 Mechelen"), None, known)
    assert (single.latitude, single.precision) == (MECHELEN_SITE[0], locate.EMPLOYER_SITE)

    two = _employer("Industrieweg 5, 2800 Mechelen", "Rue de la Loi 16, 1000 Brussels")
    assert locate.decide(bare, two, None, known) == locate.UNPLACED
    assert locate.decide(
        {**bare, "work_arrangement": "remote"}, _employer("Industrieweg 5, 2800 Mechelen"),
        None, known,
    ) == locate.UNPLACED
    assert locate.decide(
        bare, _employer("Industrieweg 5, 2800 Mechelen"), "agency", known
    ) == locate.UNPLACED
    # "Germany" is a country, so the posting names no town at all.
    assert locate.decide({"location": "Germany", "country": "DE"}, None, None, known) == (
        locate.UNPLACED
    )


def test_a_question_nobody_asked_yet_is_raised_not_guessed():
    with pytest.raises(locate._Unasked) as raised:
        locate.decide({"location": "Gent", "country": "BE"}, None, None, _known(PLACES))
    assert raised.value.key == locate.Key(locate.PLACE, "Gent", "BE")


# ---------------------------------------------------------------------------
# The job, synthesis and the cache they share
# ---------------------------------------------------------------------------


def test_the_job_places_the_backlog_and_remembers_the_answers(db, geocoder):
    seeker, campaign = _campaign()
    employer = _company("Lex NV", [{"kind": "office", "label": "Rue de la Loi 16, 1000 Brussels"}])
    agency = _company("Staffing BV", [{"kind": "office", "label": "Rue de la Loi 16, 1000 Brussels"}],
                      role="agency")
    rows = {
        "office": _opportunity(seeker, campaign, "Brussels", company_id=employer),
        "town": _opportunity(seeker, campaign, "Leuven"),
        "site": _opportunity(seeker, campaign, "BE", company_id=employer),
        "agency": _opportunity(seeker, campaign, "Brussels", company_id=agency),
        "country": _opportunity(seeker, campaign, "Germany", country="DE"),
        "remote": _opportunity(seeker, campaign, "Remote job", work_arrangement="remote"),
    }

    report = asyncio.run(locate.locate_pending())

    placed = {
        name: query_one(
            "SELECT latitude, longitude, location_precision, located_at FROM opportunity "
            "WHERE id = ?", (row_id,),
        )
        for name, row_id in rows.items()
    }
    assert placed["office"]["location_precision"] == locate.ADDRESS
    assert placed["office"]["latitude"] == RUE_DE_LA_LOI[0]
    assert placed["town"]["location_precision"] == locate.LOCALITY
    assert placed["site"]["location_precision"] == locate.EMPLOYER_SITE
    assert placed["agency"]["location_precision"] == locate.LOCALITY
    for name in ("country", "remote"):
        assert placed[name]["latitude"] is None and placed[name]["located_at"], name
    assert report["decided"] == len(rows) and report["unplaced"] == 2
    assert locate.pending_count() == 0

    # Shared knowledge: the next Brussels posting is placed on arrival, with
    # no question asked.
    asked_before = len(geocoder)
    record = locate.place_now(
        {"location": "Brussels", "country": "BE", "company_id": employer},
        query_one("SELECT * FROM company WHERE id = ?", (employer,)),
    )
    assert record["location_precision"] == locate.ADDRESS and record["located_at"]
    assert len(geocoder) == asked_before
    assert query_one("SELECT latitude FROM geo_place WHERE query_key = ?",
                     ("place:DE:germany",))["latitude"] is None


def test_synthesis_places_what_it_can_and_leaves_the_rest_to_the_job(db, geocoder):
    seeker, campaign = _campaign()
    asyncio.run(locate.Gazetteer().ask(locate.Key(locate.PLACE, "Brussels", "BE"), None))
    employer = _company("Lex NV", [{"kind": "office", "label": "Rue de la Loi 16, 1000 Brussels"}])
    company = query_one("SELECT * FROM company WHERE id = ?", (employer,))

    # The town is on record but the employer's address is not: placed at the
    # town now, and left in the queue so the job can make it exact.
    partial = locate.place_now(
        {"location": "Brussels", "country": "BE", "company_id": employer}, company
    )
    assert partial["location_precision"] == locate.LOCALITY and partial["located_at"] is None

    unknown = locate.place_now({"location": "Leuven", "country": "BE"}, None)
    assert unknown["latitude"] is None and unknown["located_at"] is None

    # Coordinates the posting carries are kept as they are.
    sourced = locate.place_now({"location": "x", "latitude": 50.0, "longitude": 4.0}, None)
    assert sourced["location_precision"] == locate.SOURCE and sourced["located_at"]


def test_a_refresh_keeps_the_placement_unless_the_row_moved(db, geocoder):
    seeker, campaign = _campaign()
    record = {"kind": "vacancy", "title": "Data Lead", "location": "Brussels", "country": "BE"}
    row_id, _ = repo.upsert_synthesised(seeker, campaign, dict(record), None)
    asyncio.run(locate.locate_pending())
    placed = query_one("SELECT * FROM opportunity WHERE id = ?", (row_id,))
    assert placed["location_precision"] == locate.LOCALITY

    # Synthesis runs again knowing nothing new: the job's answer stays.
    refresh = {**record, "latitude": None, "longitude": None,
               "location_precision": None, "located_at": None}
    repo.upsert_synthesised(seeker, campaign, refresh, placed)
    kept = query_one("SELECT * FROM opportunity WHERE id = ?", (row_id,))
    assert kept["latitude"] == BRUSSELS[0] and kept["located_at"]

    # The posting moved: placed again from scratch.
    repo.upsert_synthesised(seeker, campaign, {**refresh, "location": "Leuven"}, kept)
    moved = query_one("SELECT * FROM opportunity WHERE id = ?", (row_id,))
    assert moved["latitude"] is None and moved["located_at"] is None


def test_an_unanswering_geocoder_leaves_the_rest_queued(db, monkeypatch, geocoder):
    seeker, campaign = _campaign()
    _opportunity(seeker, campaign, "Brussels")

    async def down(query, **_):
        raise geo.GeocoderUnavailable("HTTP 429")

    monkeypatch.setattr(geo, "search", down)
    report = asyncio.run(locate.locate_pending())

    assert report["unavailable"] == "HTTP 429"
    assert locate.pending_count() == 1
    # "Nobody answered" is not "no such place": nothing is recorded.
    assert query_all("SELECT * FROM geo_place") == []


def test_coverage_says_what_the_radius_cannot_judge(db, geocoder):
    seeker, campaign = _campaign()
    _opportunity(seeker, campaign, "Brussels")
    _opportunity(seeker, campaign, "Germany", country="DE")
    _opportunity(seeker, campaign, "Remote job", work_arrangement="remote")
    _opportunity(seeker, campaign, "Leuven")
    asyncio.run(locate.locate_pending())
    _opportunity(seeker, campaign, "Gent")

    assert locate.coverage(seeker) == {
        "total": 5, "located": 2, "pending": 1, "remote_without_place": 1, "unplaced": 1,
    }


def test_the_scheduler_keeps_one_job_going(db, monkeypatch, geocoder):
    from dreamjob.jobs.runner import runner

    started: list[str] = []

    async def start(job_id, worker=None):
        started.append(job_id)
        update_row("job_run", job_id, {"status": "running"})

    monkeypatch.setattr(runner, "start", start)
    monkeypatch.setattr(runner, "is_running", lambda job_id: job_id in started)

    assert asyncio.run(locate.ensure_running()) == {"pending": 0}

    seeker, campaign = _campaign()
    _opportunity(seeker, campaign, "Brussels")
    first = asyncio.run(locate.ensure_running())
    assert first["started"] == started[0]
    assert asyncio.run(locate.ensure_running())["running"] == started[0]

    # After the geocoder stopped answering, it is left alone for a while.
    update_row("job_run", started[0], {
        "status": "done", "finished_at": utcnow(),
        "checkpoint": json.dumps({"report": {"unavailable": "HTTP 429"}}),
    })
    assert "waiting" in asyncio.run(locate.ensure_running())
    # ...and so is a run that failed outright, rather than restarted every tick.
    update_row("job_run", started[0], {"status": "failed", "checkpoint": "{}"})
    assert asyncio.run(locate.ensure_running())["waiting"] == "the last run failed"
    assert len(started) == 1


# ---------------------------------------------------------------------------
# The geocoder client
# ---------------------------------------------------------------------------


def test_a_rate_limited_geocoder_is_unavailable_not_empty(db, monkeypatch):
    monkeypatch.setattr(geo, "MIN_REQUEST_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(geo, "_memo", {})

    class Client:
        calls = 0

        async def fetch(self, url, **_):
            Client.calls += 1
            return types.SimpleNamespace(ok=False, status_code=429, text="", from_cache=False)

    with pytest.raises(geo.GeocoderUnavailable):
        asyncio.run(geo.geocode("Brussels", egress=Client(), strict=True))
    # Without ``strict`` the caller still gets the empty answer it always got,
    # and the refusal is not remembered as the answer.
    assert asyncio.run(geo.geocode("Brussels", egress=Client())) is None
    assert asyncio.run(geo.geocode("Brussels", egress=Client())) is None
    assert Client.calls == 3


def test_the_place_rank_is_read_from_the_answer():
    result = geo._to_result("Brussels", {"lat": "50.8", "lon": "4.3", "place_rank": 16})
    assert result.place_rank == 16
