# path: tests/test_release_ranking.py
"""NZB release ranking: language must not discard results.

Regression coverage for: the language check dropped any release whose title
contained a nationality substring, so legitimate English books were discarded and
the requester was told "No suitable English results". Verified against real
releases, 7 of 9 genuine English books were being thrown away.
"""
import conftest  # noqa: F401

from plugins.media_requests.cog import LANGUAGE_TAGS, looks_foreign_language

# Real English-language books, named the way usenet releases name them.
ENGLISH_RELEASES = [
    "The.Dutch.House.Ann.Patchett.2019.Retail.EPUB.eBook-BitBook",
    "The.German.Wife.Kelly.Rimmer.2022.EPUB",
    "The.French.Lieutenants.Woman.John.Fowles.EPUB",
    "Make.It.Stick.The.Science.of.Successful.Learning.EPUB",
    "Russian.Roulette.Anthony.Horowitz.EPUB",
    "The.Japanese.Lover.Isabel.Allende.EPUB",
    "Project.Hail.Mary.Andy.Weir.2021.Retail.EPUB",
    "Red.Rising.Pierce.Brown.2014.EPUB",
    "Polish.Your.Prose.A.Writing.Guide.EPUB",
]


def _rank(candidates, format_type="ebook"):
    """Mirror the sort in _submit_to_download."""
    if format_type != "audiobook":
        key = lambda c: (not c["is_foreign"], c["has_epub"], c["grabs"])
    else:
        key = lambda c: (not c["is_foreign"], c["grabs"])
    return sorted(candidates, key=key, reverse=True)


def _candidate(title, grabs=0):
    return {
        "title": title,
        "grabs": grabs,
        "has_epub": "epub" in title.lower(),
        "is_foreign": looks_foreign_language(title),
        "nzb_url": "http://example/x.nzb",
    }


# --- the false positives that used to be silently dropped ---

def test_two_letter_codes_no_longer_trip_the_check():
    """'.it.' matched 'Make.It.Stick'; the codes are gone entirely."""
    assert looks_foreign_language("Make.It.Stick.The.Science.of.Learning") is False
    for code in ("de", "fr", "es", "it", "nl", "pt"):
        assert code not in LANGUAGE_TAGS


def test_no_english_release_is_ever_discarded():
    """The core fix: language is a ranking signal, so nothing is filtered out."""
    candidates = [_candidate(t) for t in ENGLISH_RELEASES]
    assert len(_rank(candidates)) == len(ENGLISH_RELEASES)


def test_clean_english_titles_are_not_flagged():
    for title in ("Project.Hail.Mary.Andy.Weir.2021.Retail.EPUB",
                  "Red.Rising.Pierce.Brown.2014.EPUB",
                  "Make.It.Stick.The.Science.of.Successful.Learning.EPUB"):
        assert looks_foreign_language(title) is False, title


# --- ranking behaviour ---

def test_english_outranks_flagged_even_with_fewer_grabs():
    candidates = [
        _candidate("Der.Schwarm.German.Retail.EPUB", grabs=900),
        _candidate("The.Swarm.Frank.Schaetzing.EPUB", grabs=5),
    ]
    assert _rank(candidates)[0]["title"].startswith("The.Swarm")


def test_grabs_still_decide_among_equals():
    candidates = [
        _candidate("Some.Book.EPUB", grabs=10),
        _candidate("Some.Book.Other.Release.EPUB", grabs=999),
    ]
    assert _rank(candidates)[0]["grabs"] == 999


def test_epub_preferred_for_ebooks():
    candidates = [
        _candidate("Some.Book.MOBI", grabs=50),
        _candidate("Some.Book.EPUB", grabs=50),
    ]
    assert _rank(candidates, "ebook")[0]["has_epub"] is True


def test_audiobook_ranking_ignores_epub_bonus():
    candidates = [
        _candidate("Some.Audiobook.M4B", grabs=100),
        _candidate("Some.Audiobook.EPUB", grabs=5),
    ]
    assert _rank(candidates, "audiobook")[0]["grabs"] == 100


def test_flagged_release_is_still_downloadable_as_last_resort():
    """Previously this produced 'no results'; now it is chosen, with a warning."""
    candidates = [_candidate("Der.Schwarm.German.Retail.EPUB", grabs=900)]
    assert _rank(candidates)[0]["is_foreign"] is True


def test_genuine_foreign_tag_is_still_detected():
    assert looks_foreign_language("Der.Schwarm.German.Retail.EPUB") is True
    assert looks_foreign_language("Le.Petit.Prince.French.EPUB") is True


def test_whole_token_matching_only():
    """Substring matching is what caused the damage; confirm it is gone."""
    assert looks_foreign_language("Germanium.Chemistry.Textbook.EPUB") is False
    assert looks_foreign_language("Polished.Chrome.EPUB") is False


# --- the search itself: NZBHydra's JSON answer, through the shared client ---

def _item(title, link, grabs=None, attr_key="attr"):
    item = {"title": title, "link": link, "pubDate": "Thu, 01 Oct 2026 14:09:28 +0000",
            "enclosure": {"@attributes": {"url": link, "length": "1048576", "type": "application/x-nzb"}}}
    if grabs is not None:
        item[attr_key] = [{"@attributes": {"name": "category", "value": "7020"}},
                          {"@attributes": {"name": "grabs", "value": str(grabs)}}]
    return item


def _feed(*items):
    """An answer shaped like NZBHydra's o=json newznab output."""
    return {"channel": {"title": "NZBHydra", "response": {"@attributes": {"offset": 0, "total": len(items)}},
                        "item": list(items)}}


class _Response:
    status, content_length = 200, None

    def __init__(self, body):
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, **kw):
        return self.body

    async def text(self):
        import json
        return json.dumps(self.body)


class _Http:
    """NZBHydra at http://hydra:5076 and SABnzbd at http://sab:8080; `asked` records
    (url, params) for each call."""

    def __init__(self, hydra_body, sab_body=None):
        self.hydra_body, self.sab_body = hydra_body, sab_body or {"status": True, "nzo_ids": ["SABnzbd_nzo_1"]}
        self.asked = []

    def request(self, method, url, params=None, **kw):
        self.asked.append((url, dict(params or {})))
        return _Response(self.hydra_body if url.startswith("http://hydra") else self.sab_body)


def _services(http):
    from types import SimpleNamespace

    from core.clients import Hydra, Sabnzbd

    config = SimpleNamespace(nzbhydra_url="http://hydra:5076", nzbhydra_api_key="hydra-key",
                             sabnzbd_url="http://sab:8080", sabnzbd_api_key="sab-key",
                             bookshelf_ebook_watch="", bookshelf_audiobook_watch="")
    services = SimpleNamespace(config=config, http_session=http)
    services.hydra, services.sab = Hydra(services), Sabnzbd(services)
    return services


def test_hydra_search_reads_grabs_from_the_newznab_attributes():
    import asyncio

    services = _services(_Http(_feed(_item("Some.Book.EPUB", "http://hydra/getnzb/1", grabs=42),
                                     _item("Some.Book.MOBI", "http://hydra/getnzb/2"))))
    out = asyncio.run(services.hydra.search("Some Book", ["7020"], limit=20, timeout=30))
    assert [(r["title"], r["grabs"]) for r in out] == [("Some.Book.EPUB", 42), ("Some.Book.MOBI", 0)]
    assert out[0]["link"] == "http://hydra/getnzb/1" and out[0]["size"] == 1048576


def test_hydra_search_reads_grabs_written_as_newznab_attr_too():
    import asyncio

    services = _services(_Http(_feed(_item("Some.Book.EPUB", "http://hydra/getnzb/1", grabs=7,
                                           attr_key="newznab:attr"))))
    out = asyncio.run(services.hydra.search("Some Book", ["7020"]))
    assert out[0]["grabs"] == 7


def test_a_malformed_result_does_not_hide_the_good_ones():
    """One result with no title, an odd size or no shape at all must not cost the others."""
    import asyncio

    untitled, blank = {"link": "http://hydra/getnzb/0"}, _item("   ", "http://hydra/getnzb/9")
    odd_size = _item("Project.Hail.Mary.MOBI", "http://hydra/getnzb/2", grabs=1)
    odd_size["enclosure"]["@attributes"]["length"] = "1.2 MB"
    services = _services(_Http(_feed(untitled, None, blank, odd_size,
                                     _item(" Project.Hail.Mary.EPUB ", "http://hydra/getnzb/1", grabs=3))))
    out = asyncio.run(services.hydra.search("Project Hail Mary", ["7020"]))
    assert [(r["title"], r["size"]) for r in out] == [("Project.Hail.Mary.MOBI", 0), ("Project.Hail.Mary.EPUB", 1048576)]


def _book_view(services, request_format="ebook"):
    from plugins.media_requests.cog import BookAdminApprovalView

    return BookAdminApprovalView({"title": "The Swarm", "author": "Frank Schaetzing",
                                  "request_format": request_format}, 7, services)


def test_book_download_searches_through_the_shared_client_and_sends_the_best_release():
    import asyncio

    http = _Http(_feed(_item("Der.Schwarm.German.Retail.EPUB", "http://hydra/getnzb/de", grabs=900),
                       _item("The.Swarm.Frank.Schaetzing.MOBI", "http://hydra/getnzb/mobi", grabs=80),
                       _item("The.Swarm.Frank.Schaetzing.EPUB", "http://hydra/getnzb/epub", grabs=5)))
    assert asyncio.run(_book_view(_services(http))._submit_to_download()) is True
    (hydra_url, search), (sab_url, sab) = http.asked
    assert hydra_url == "http://hydra:5076/api"
    assert (search["t"], search["q"], search["cat"], search["limit"], search["o"]) == (
        "search", "The Swarm Frank Schaetzing", "7020", "20", "json")
    assert sab_url == "http://sab:8080/api"
    assert (sab["mode"], sab["name"], sab["cat"]) == ("addurl", "http://hydra/getnzb/epub", "ebooks")


def test_audiobook_download_ranks_by_grabs_in_the_audiobook_category():
    import asyncio

    http = _Http(_feed(_item("The.Swarm.Audiobook.EPUB", "http://hydra/getnzb/a", grabs=5),
                       _item("The.Swarm.Audiobook.M4B", "http://hydra/getnzb/b", grabs=100)))
    assert asyncio.run(_book_view(_services(http), "audiobook")._submit_to_download()) is True
    (_, search), (_, sab) = http.asked
    assert search["cat"] == "3030"
    assert (sab["name"], sab["cat"]) == ("http://hydra/getnzb/b", "audiobooks")


def test_book_download_stops_when_nzbhydra_refuses_the_search():
    import asyncio

    http = _Http({"error": {"@attributes": {"code": "100", "description": "Incorrect user credentials"}}})
    assert asyncio.run(_book_view(_services(http))._submit_to_download()) is False
    assert [url for url, _ in http.asked] == ["http://hydra:5076/api"]


def test_book_download_with_no_results_sends_nothing():
    import asyncio

    http = _Http(_feed())
    assert asyncio.run(_book_view(_services(http))._submit_to_download()) is False
    assert [url for url, _ in http.asked] == ["http://hydra:5076/api"]


def test_language_check_is_not_used_as_a_filter():
    """Guard the intent: no `continue` on the language result."""
    import inspect

    from plugins.media_requests.cog import BookAdminApprovalView

    source = inspect.getsource(BookAdminApprovalView._submit_to_download)
    assert "foreign_indicators" not in source, "old substring filter still present"
    assert "is_foreign = looks_foreign_language" in source
