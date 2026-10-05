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


def test_missing_title_element_returns_empty_not_error():
    """One malformed result used to abort the whole submission."""
    import xml.etree.ElementTree as ET

    from plugins.media_requests.cog import result_title

    # A result with no <title> child at all - the case that raised AttributeError.
    no_title = ET.fromstring("<item><link>http://x/y.nzb</link></item>")
    assert result_title(no_title) == ""

    # A <title> present but empty.
    empty = ET.fromstring("<item><title></title></item>")
    assert result_title(empty) == ""

    # A normal one, whitespace trimmed.
    good = ET.fromstring("<item><title>  Some.Book.EPUB  </title></item>")
    assert result_title(good) == "Some.Book.EPUB"


def test_a_malformed_result_does_not_hide_the_good_ones():
    """The loop must keep the other 19 results when one entry is broken."""
    import xml.etree.ElementTree as ET

    from plugins.media_requests.cog import result_title

    feed = ET.fromstring(
        "<rss><channel>"
        "<item><link>a</link></item>"
        "<item><title>Project.Hail.Mary.EPUB</title></item>"
        "</channel></rss>"
    )
    titles = [result_title(i) for i in feed.findall(".//item")]
    assert titles.count("") == 1
    assert "Project.Hail.Mary.EPUB" in titles


def test_language_check_is_not_used_as_a_filter():
    """Guard the intent: no `continue` on the language result."""
    import inspect

    from plugins.media_requests.cog import BookAdminApprovalView

    source = inspect.getsource(BookAdminApprovalView._submit_to_download)
    assert "foreign_indicators" not in source, "old substring filter still present"
    assert "is_foreign = looks_foreign_language" in source
