# Naming: `skein` vs `interskein`

A standing rule (Patrick, 2026-06-23), not specific to any one stage. It decides
which of the two names a thing carries.

## The rule

**`interskein`** is used ONLY where the name is forced on us by an external
namespace we cannot otherwise claim — `skein` is squatted there. Concretely:

- the **domain** — `interskein.com`
- the **public web station** served at that domain (its public identity)
- the **PyPI distribution name** — `pyproject.toml` `name = "interskein"`
- the **protocol / mesh layer** as a published concept (the federation/mesh
  identity that travels between stations)

**`skein`** is used for everything else — everything internal, everything we
control:

- the **CLI command** (`interskein …` → `skein …`)
- the **import package** (`skein_next` / legacy `skein` → `skein`)
- **environment variables** (`SKEIN_NEXT_*` → `SKEIN_*`)
- the **data directory** (`.skein-next` → `.skein`)
- internal module names, variable names, docstrings, comments, examples

## Consequence

Distribution name ≠ import name, on purpose:

```
pip install interskein      # the PyPI name (skein is squatted there)
skein post finding …        # the CLI command
import skein                # the Python package
```

This is a normal, supported split (cf. `pip install pillow` → `import PIL`). The
PyPI name and the domain stay `interskein` because we can't have `skein` there;
nothing about the local tool's identity is `interskein`.

## Why write it down

The cutover (port Stage 4) renames ~hundreds of `skein_next` / `interskein`
references at once. Without the rule encoded, a blanket find-and-replace would
either rename the PyPI/domain identity (breaking install + deploy) or leave the
CLI/package half-named. The rule is the filter that says which references move
and which stay.
