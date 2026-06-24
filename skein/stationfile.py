"""The stationfile — a published station's identity and presentation config.

Per brief-20260603-s4gq (layer 2). The stationfile is the station-side sibling of
the *mefile* (the participant's local config): same machinery — a schema-versioned
canonical JSON file — different scope. It lives at ``.skein/stationfile.json``,
next to ``store.db`` in the data dir, and is mounted read-only into the container
alongside the corpus, so a config or theme change rides the LIGHT deploy path
(edit + ``publish.sh`` force-recreate, no docker build).

Validation posture is **ease, not enforcement** (§ "Validation posture"):

- ``name`` is the ONLY hard requirement. No name from anywhere — no stationfile,
  empty ``name``, and no ``SKEIN_PROJECT`` bootstrap env — is a hard error
  (:class:`StationfileError`); the station refuses to start rather than silently
  inherit a blank or invented label. Naming a station is basic, not a11y-policing.
- Everything else degrades and logs a warning: a bad ``theme`` path falls back to
  the default, an unknown/bad token is dropped. No contrast or a11y gating —
  owners own their station's look.
- ``schema_version`` drives forward migration. A version we don't know how to read
  is a loud error, never a silent misread.

**Name precedence.** ``stationfile.name`` wins; the ``SKEIN_PROJECT`` env var
is a bootstrap that supplies the name *until a stationfile exists*, then steps
aside. This deviates from a literal reading of the spec's "defaults < file < env"
line in favor of its "env sets name until a stationfile exists" line: the
stationfile's whole purpose is to be changed via the light deploy path, so a
baked-in compose env var must not override it (that would force a code-path deploy
to rename a station — the exact pain the stationfile exists to avoid).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union

logger = logging.getLogger(__name__)

STATIONFILE_NAME = "stationfile.json"

# The only schema version this build understands. A stationfile declaring a newer
# version is a hard error (we cannot know what its fields mean); an older version
# would be handled by a migration step when one exists.
SCHEMA_VERSION = 1

DEFAULT_THEME = "ulm"
# The themes shipped in skein/web/static/themes/. A `theme` naming one of
# these is served from the package; anything else is treated as a custom-sheet
# path relative to the data dir (level 2 of the theming ladder).
SHIPPED_THEMES = frozenset({"ulm", "classic"})

# The token surface (§ layer 3, level 0): the no-CSS path. Each maps to a CSS
# custom property the base sheet is authored against. Unknown keys are dropped
# with a warning so a typo degrades visibly rather than silently doing nothing.
KNOWN_TOKENS = frozenset({"accent", "font_body", "font_mono", "default_theme"})
_VALID_DEFAULT_THEME = frozenset({"light", "dark"})

# Token values are written into a `<style>` block as CSS custom-property values.
# These characters would let a value escape its declaration (`;`/`{`/`}`) or the
# style element itself (`<`/`>`) — so a value carrying any of them is dropped.
# An owner who wants arbitrary CSS uses a custom theme sheet (level 2); the token
# path stays a safe single-value override. Legitimate colors and font stacks
# never contain these.
_TOKEN_FORBIDDEN = frozenset("<>{};")


class StationfileError(RuntimeError):
    """A stationfile problem that must stop the station from starting.

    Raised only for the hard failures: a missing/empty ``name`` with no env
    bootstrap, malformed JSON, or an unreadable future ``schema_version``.
    Everything else degrades with a warning instead.
    """


@dataclass(frozen=True)
class StationConfig:
    """The resolved, validated station identity + presentation.

    ``name`` is always a non-empty string (the loader guarantees it). ``theme`` is
    either a shipped theme name (in :data:`SHIPPED_THEMES`) or a custom-sheet path
    relative to the data dir. ``tokens`` holds only the known, well-typed token
    overrides — the base sheet reads them as CSS custom properties.
    """

    name: str
    tagline: Optional[str] = None
    logo: Optional[str] = None
    theme: str = DEFAULT_THEME
    tokens: Dict[str, str] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    @property
    def is_shipped_theme(self) -> bool:
        return self.theme in SHIPPED_THEMES


def _stationfile_path(data_dir: Optional[Union[str, Path]]) -> Optional[Path]:
    if data_dir is None:
        return None
    return Path(data_dir) / STATIONFILE_NAME


def _read_raw(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    """Read and JSON-parse the stationfile, or ``None`` when there is none.

    A present-but-malformed stationfile is a hard error: a station configured with
    broken JSON should fail loudly, not fall through to the env bootstrap as if no
    config were written at all.
    """
    if path is None or not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise StationfileError(f"could not read stationfile at {path}: {e}") from e
    try:
        raw = json.loads(text)
    except ValueError as e:
        raise StationfileError(f"stationfile at {path} is not valid JSON: {e}") from e
    if not isinstance(raw, Mapping):
        raise StationfileError(
            f"stationfile at {path} must be a JSON object, got {type(raw).__name__}"
        )
    return dict(raw)


def _check_schema_version(raw: Mapping[str, Any], path: Optional[Path]) -> int:
    """Validate ``schema_version``; a newer-than-known version is a hard error."""
    version = raw.get("schema_version", SCHEMA_VERSION)
    if not isinstance(version, int) or isinstance(version, bool):
        raise StationfileError(
            f"stationfile at {path}: schema_version must be an integer, got {version!r}"
        )
    if version > SCHEMA_VERSION:
        raise StationfileError(
            f"stationfile at {path}: schema_version {version} is newer than this "
            f"build understands (max {SCHEMA_VERSION}); upgrade the station code"
        )
    return version


def _resolve_name(file_name: Any, env_name: Optional[str], path: Optional[Path]) -> str:
    """Resolve the station name: stationfile wins, env bootstraps, else hard fail."""
    for candidate in (file_name, env_name):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    where = f" at {path}" if path is not None else ""
    raise StationfileError(
        "station has no name: set 'name' in the stationfile"
        f"{where} or the SKEIN_PROJECT env var. A station must be named "
        "(it is basic identity, not accessibility policing)."
    )


def _resolve_theme(raw: Mapping[str, Any], data_dir: Optional[Union[str, Path]]) -> str:
    """Resolve and validate the theme, degrading to the default with a warning.

    A shipped-theme name is taken as-is. Any other value is read as a custom-sheet
    path relative to the data dir; if that file is absent (or the path escapes the
    data dir, or the data dir is unknown) the theme falls back to the default —
    the page still renders, just unthemed-custom.
    """
    value = raw.get("theme")
    if value is None:
        return DEFAULT_THEME
    if not isinstance(value, str) or not value.strip():
        logger.warning("stationfile: theme must be a non-empty string; using %r", DEFAULT_THEME)
        return DEFAULT_THEME
    value = value.strip()
    if value in SHIPPED_THEMES:
        return value
    # A custom sheet: must resolve to a real file inside the data dir.
    if data_dir is None:
        logger.warning(
            "stationfile: custom theme %r needs a data dir to resolve against; using %r",
            value, DEFAULT_THEME,
        )
        return DEFAULT_THEME
    base = Path(data_dir).resolve()
    try:
        target = (base / value).resolve()
        target.relative_to(base)  # reject path traversal out of the data dir
    except (ValueError, OSError):
        logger.warning("stationfile: custom theme path %r is invalid; using %r", value, DEFAULT_THEME)
        return DEFAULT_THEME
    if not target.is_file():
        logger.warning("stationfile: custom theme %r not found; using %r", value, DEFAULT_THEME)
        return DEFAULT_THEME
    return value


def _resolve_tokens(raw: Mapping[str, Any]) -> Dict[str, str]:
    """Keep the known, well-typed token overrides; drop+warn on anything else."""
    tokens_in = raw.get("tokens")
    if tokens_in is None:
        return {}
    if not isinstance(tokens_in, Mapping):
        logger.warning("stationfile: tokens must be an object; ignoring %r", tokens_in)
        return {}
    out: Dict[str, str] = {}
    for key, val in tokens_in.items():
        if key not in KNOWN_TOKENS:
            logger.warning("stationfile: unknown token %r ignored", key)
            continue
        if not isinstance(val, str) or not val.strip():
            logger.warning("stationfile: token %r must be a non-empty string; ignored", key)
            continue
        val = val.strip()
        if key != "default_theme" and any(c in _TOKEN_FORBIDDEN for c in val):
            logger.warning(
                "stationfile: token %r value contains a disallowed character "
                "(<>{};) and was dropped; use a custom theme sheet for arbitrary CSS",
                key,
            )
            continue
        if key == "default_theme" and val not in _VALID_DEFAULT_THEME:
            logger.warning(
                "stationfile: default_theme must be one of %s; ignored",
                sorted(_VALID_DEFAULT_THEME),
            )
            continue
        out[key] = val
    return out


def _optional_str(raw: Mapping[str, Any], key: str) -> Optional[str]:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        logger.warning("stationfile: %s must be a non-empty string; ignored", key)
        return None
    return value.strip()


def load_station_config(
    data_dir: Optional[Union[str, Path]] = None,
    env_name: Optional[str] = None,
) -> StationConfig:
    """Load + validate the stationfile into a :class:`StationConfig`.

    ``data_dir`` is the ``.skein`` dir (where ``store.db`` lives); the
    stationfile is read from ``data_dir/stationfile.json``. ``env_name`` is the
    ``SKEIN_PROJECT`` bootstrap value (the caller reads the env).

    Raises :class:`StationfileError` for the hard failures (no resolvable name,
    malformed JSON, unreadable future schema). Soft problems (bad theme path,
    junk tokens) degrade with a logged warning.
    """
    path = _stationfile_path(data_dir)
    raw = _read_raw(path)
    if raw is None:
        # No stationfile: only the env bootstrap can supply a name.
        name = _resolve_name(None, env_name, path)
        return StationConfig(name=name)

    _check_schema_version(raw, path)
    name = _resolve_name(raw.get("name"), env_name, path)
    return StationConfig(
        name=name,
        tagline=_optional_str(raw, "tagline"),
        logo=_optional_str(raw, "logo"),
        theme=_resolve_theme(raw, data_dir),
        tokens=_resolve_tokens(raw),
        schema_version=SCHEMA_VERSION,
    )
