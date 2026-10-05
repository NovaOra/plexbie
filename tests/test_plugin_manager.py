# path: tests/test_plugin_manager.py
"""Plugin cog-name derivation.

Regression coverage for: the loader derived the cog class name with a PascalCase
join while the (since removed) unloader used str.title(), producing
"Watch_TrackingCog" for any multi-word plugin, so the two never agreed.
"""
import conftest  # noqa: F401

from core.plugin_manager import cog_class_name


# --- the name derivation ---

def test_multi_word_plugin_name():
    assert cog_class_name("watch_tracking") == "WatchTrackingCog"


def test_three_word_plugin_name():
    assert cog_class_name("bookshelf_processor_x") == "BookshelfProcessorXCog"


def test_single_word_plugin_name():
    assert cog_class_name("status") == "StatusCog"


def test_title_case_bug_is_not_reintroduced():
    """str.title() produced Watch_TrackingCog - the underscore is the tell."""
    assert "_" not in cog_class_name("watch_tracking")


def test_derivation_matches_every_real_plugin():
    """The real cog classes must be findable by the derived name."""
    import importlib
    import pathlib

    plugins_dir = pathlib.Path(conftest.PROJECT_ROOT) / "plugins"
    checked = 0
    for plugin_dir in sorted(plugins_dir.iterdir()):
        if not plugin_dir.is_dir() or not (plugin_dir / "cog.py").exists():
            continue
        module = importlib.import_module(f"plugins.{plugin_dir.name}.cog")
        expected = cog_class_name(plugin_dir.name)
        assert hasattr(module, expected), (
            f"plugins/{plugin_dir.name}/cog.py has no class {expected}"
        )
        checked += 1
    assert checked >= 11, f"expected to check the full plugin set, got {checked}"
