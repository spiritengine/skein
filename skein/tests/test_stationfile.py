"""Tests for the stationfile loader (brief-20260603-s4gq, slice 3).

The contract under test is the validation posture: ``name`` is the one hard
requirement (no name anywhere -> StationfileError), and everything else degrades
with a warning rather than failing. Plus the name precedence call — stationfile
wins, env bootstraps until a stationfile exists — and the forward-migration
guard on ``schema_version``.
"""

import json

import pytest

from skein.stationfile import (
    DEFAULT_THEME,
    SCHEMA_VERSION,
    StationConfig,
    StationfileError,
    load_station_config,
)


def _write(data_dir, obj):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "stationfile.json").write_text(json.dumps(obj), encoding="utf-8")


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / ".skein"


# --- name: the one hard requirement -----------------------------------------


def test_name_from_stationfile(data_dir):
    _write(data_dir, {"name": "Field Notes"})
    cfg = load_station_config(data_dir)
    assert cfg.name == "Field Notes"
    assert cfg.theme == DEFAULT_THEME  # default when unspecified
    assert cfg.tokens == {}


def test_name_from_env_bootstrap_when_no_stationfile(data_dir):
    # No stationfile at all: the env bootstrap supplies the name.
    cfg = load_station_config(data_dir, env_name="interskein")
    assert cfg.name == "interskein"


def test_stationfile_name_wins_over_env(data_dir):
    # Precedence call: the stationfile is the real config; the env is bootstrap
    # that steps aside once a stationfile exists (so a rename rides the light
    # deploy path, not a code deploy).
    _write(data_dir, {"name": "Field Notes"})
    cfg = load_station_config(data_dir, env_name="interskein")
    assert cfg.name == "Field Notes"


def test_no_name_anywhere_is_hard_error(data_dir):
    # No stationfile and no env bootstrap -> refuse to start.
    with pytest.raises(StationfileError):
        load_station_config(data_dir)


def test_empty_name_in_stationfile_falls_to_env(data_dir):
    _write(data_dir, {"name": "   "})
    cfg = load_station_config(data_dir, env_name="interskein")
    assert cfg.name == "interskein"


def test_empty_name_and_no_env_is_hard_error(data_dir):
    _write(data_dir, {"name": ""})
    with pytest.raises(StationfileError):
        load_station_config(data_dir)


def test_name_is_stripped(data_dir):
    _write(data_dir, {"name": "  Field Notes  "})
    assert load_station_config(data_dir).name == "Field Notes"


# --- malformed file / schema version: hard errors ---------------------------


def test_malformed_json_is_hard_error(data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "stationfile.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(StationfileError):
        load_station_config(data_dir, env_name="fallback")


def test_non_object_json_is_hard_error(data_dir):
    _write(data_dir, ["not", "an", "object"])
    with pytest.raises(StationfileError):
        load_station_config(data_dir, env_name="fallback")


def test_future_schema_version_is_hard_error(data_dir):
    _write(data_dir, {"name": "X", "schema_version": SCHEMA_VERSION + 1})
    with pytest.raises(StationfileError):
        load_station_config(data_dir)


def test_non_int_schema_version_is_hard_error(data_dir):
    _write(data_dir, {"name": "X", "schema_version": "1"})
    with pytest.raises(StationfileError):
        load_station_config(data_dir)


# --- theme: degrades, never fails -------------------------------------------


def test_shipped_theme_accepted(data_dir):
    _write(data_dir, {"name": "X", "theme": "classic"})
    assert load_station_config(data_dir).theme == "classic"


def test_unknown_theme_degrades_to_default(data_dir):
    _write(data_dir, {"name": "X", "theme": "nope-not-a-theme"})
    assert load_station_config(data_dir).theme == DEFAULT_THEME


def test_custom_theme_path_accepted_when_file_present(data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    themes = data_dir / "themes"
    themes.mkdir()
    (themes / "mine.css").write_text("/* custom */", encoding="utf-8")
    _write(data_dir, {"name": "X", "theme": "themes/mine.css"})
    assert load_station_config(data_dir).theme == "themes/mine.css"


def test_custom_theme_path_missing_degrades_to_default(data_dir):
    _write(data_dir, {"name": "X", "theme": "themes/absent.css"})
    assert load_station_config(data_dir).theme == DEFAULT_THEME


def test_custom_theme_path_traversal_rejected(data_dir):
    # A path escaping the data dir must not resolve, even if a file exists there.
    _write(data_dir, {"name": "X", "theme": "../../etc/passwd"})
    assert load_station_config(data_dir).theme == DEFAULT_THEME


# --- tokens: keep the known, drop the rest ----------------------------------


def test_known_tokens_kept(data_dir):
    _write(data_dir, {"name": "X", "tokens": {"accent": "#0a7d3b", "default_theme": "dark"}})
    cfg = load_station_config(data_dir)
    assert cfg.tokens == {"accent": "#0a7d3b", "default_theme": "dark"}


def test_unknown_token_dropped(data_dir):
    _write(data_dir, {"name": "X", "tokens": {"accent": "#fff", "bogus": "x"}})
    assert load_station_config(data_dir).tokens == {"accent": "#fff"}


def test_bad_default_theme_token_dropped(data_dir):
    _write(data_dir, {"name": "X", "tokens": {"default_theme": "chartreuse"}})
    assert load_station_config(data_dir).tokens == {}


def test_non_string_token_dropped(data_dir):
    _write(data_dir, {"name": "X", "tokens": {"accent": 123}})
    assert load_station_config(data_dir).tokens == {}


def test_token_with_css_breakout_chars_dropped(data_dir):
    # A token value goes into a <style> block; one that could escape its
    # declaration or the element is dropped (use a custom sheet for real CSS).
    _write(data_dir, {"name": "X", "tokens": {
        "accent": "red; } body { display:none",
        "font_body": "x</style><script>alert(1)</script>",
    }})
    assert load_station_config(data_dir).tokens == {}


def test_legit_font_stack_token_kept(data_dir):
    # Commas and spaces are fine; only <>{}; are forbidden.
    _write(data_dir, {"name": "X", "tokens": {"font_body": "Georgia, 'Times New Roman', serif"}})
    assert load_station_config(data_dir).tokens["font_body"] == "Georgia, 'Times New Roman', serif"


def test_tokens_not_object_ignored(data_dir):
    _write(data_dir, {"name": "X", "tokens": "not-an-object"})
    assert load_station_config(data_dir).tokens == {}


# --- optional fields --------------------------------------------------------


def test_tagline_and_logo_optional(data_dir):
    _write(data_dir, {"name": "X", "tagline": "field notes from the mesh", "logo": "text"})
    cfg = load_station_config(data_dir)
    assert cfg.tagline == "field notes from the mesh"
    assert cfg.logo == "text"


def test_blank_optional_fields_become_none(data_dir):
    _write(data_dir, {"name": "X", "tagline": "  ", "logo": ""})
    cfg = load_station_config(data_dir)
    assert cfg.tagline is None and cfg.logo is None


def test_config_is_frozen():
    cfg = StationConfig(name="X")
    with pytest.raises(Exception):
        cfg.name = "Y"  # type: ignore[misc]
