# path: tests/test_guids.py
"""Plex GUIDs: one reader for a title's TMDB / TheTVDB / IMDb ids.

The cleanup task read a title's ids off plexapi's objects in two ways (one for its
TMDB number, which also looked at the title's own guid and at tags, one for the
ids it links copies and kept titles by), and the website's countdown a third way off
Plex's XML. A title with its TMDB number in only some of those places was kept by
one and counted down by another.
"""
import xml.etree.ElementTree as ET

import conftest  # noqa: F401

from utils.guids import element_guids, first_number, item_guids, plex_ids


class _Guid:
    def __init__(self, gid=None, tag=None):
        if gid is not None:
            self.id = gid
        if tag is not None:
            self.tag = tag


class _Item:
    def __init__(self, guid=None, guids=()):
        self.guid, self.guids = guid, list(guids)


def test_a_titles_ids_are_the_first_of_each_kind():
    guids = ["plex://movie/5d776825880197001ec967c6", "imdb://tt0133093", "tmdb://603",
             "tvdb://169", "tmdb://604", "local://12", "tmdb://"]
    assert plex_ids(guids) == {"imdb": "tt0133093", "tmdb": "603", "tvdb": "169"}
    assert plex_ids([]) == {} and plex_ids([None, ""]) == {}


def test_a_tmdb_number_is_the_first_one_readable():
    assert first_number(["plex://show/5d9c", "tmdb://1396?lang=en", "tmdb://99"], "tmdb") == 1396
    assert first_number(["tmdb://soon", "tvdb://81189/1/1"], "tmdb") is None
    assert first_number(["tmdb://soon", "tvdb://81189/1/1"], "tvdb") == 81189
    assert first_number([], "tmdb") is None


def test_plexapi_and_plex_xml_give_the_same_guids():
    """A title's own guid first, then each of its guids, by id or (without one) by tag."""
    item = _Item("tmdb://603", [_Guid("imdb://tt0133093"), _Guid(tag="tvdb://169"), _Guid()])
    el = ET.fromstring('<Video guid="tmdb://603"><Guid id="imdb://tt0133093"/><Guid id="tvdb://169"/>'
                       '<Guid/></Video>')
    assert item_guids(item)[:3] == element_guids(el) == ["tmdb://603", "imdb://tt0133093", "tvdb://169"]
    assert plex_ids(item_guids(item)) == plex_ids(element_guids(el)) \
        == {"tmdb": "603", "imdb": "tt0133093", "tvdb": "169"}

    assert item_guids(_Item()) == [] and element_guids(ET.fromstring("<Directory/>")) == []

    class Bare:
        """An item plexapi gave no guid or guids at all."""
    assert item_guids(Bare()) == []
