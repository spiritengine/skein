# Station config & theming

How a published station (the `skein` read surface on :9001) gets its
identity and its look. Built in slice 3 of the mesh-web read surface
(brief-20260603-s4gq). The governing principle: the station exists so **agents**
can read it. The structured wire is the product; the HTML is one presentation of
the same envelope; themes restyle the HTML and **cannot touch the markup or the
structured path**. Owners get CSS, never the spine — so a hostile stylesheet can
only restyle, never break the semantic DOM or the machine read.

## The stationfile

A station's identity and presentation come from `.skein/stationfile.json` —
a schema-versioned JSON file sitting next to `store.db` in the data dir. Because
the data dir is mounted into the container (at `/data`), a config or theme change
rides the **light deploy path** (edit the file + `publish.sh` force-recreate, no
docker build).

```json
{
  "schema_version": 1,
  "name": "Field Notes",
  "tagline": "notes from the mesh",
  "logo": "text",
  "theme": "ulm",
  "tokens": {
    "accent": "#0a7d3b",
    "font_body": "Georgia, 'Times New Roman', serif",
    "font_mono": "ui-monospace, monospace",
    "default_theme": "light"
  }
}
```

### Validation posture — ease, not enforcement

- **`name` is the only hard requirement.** No name anywhere — no stationfile,
  empty `name`, and no `SKEIN_PROJECT` bootstrap env — and the station
  **refuses to start** (`StationfileError`). Naming a station is basic identity;
  we never invent or blank a name.
- **Everything else degrades with a logged warning.** A bad `theme` path falls
  back to the default; an unknown or malformed token is dropped; a junk optional
  field becomes empty. The page always renders.
- **`schema_version` is a forward-migration guard.** A version newer than the
  running build understands is a loud error, never a silent misread.
- **Malformed JSON is a hard error** — a station configured with broken JSON
  fails loudly rather than falling through to the env bootstrap as if unconfigured.

### Name precedence

`stationfile.name` wins; `SKEIN_PROJECT` is a **bootstrap that supplies the
name only until a stationfile exists**, then steps aside. (This favors the spec's
"env sets name until a stationfile exists" over a literal "defaults < file < env":
the stationfile's whole purpose is to be changed via the light deploy path, so a
baked-in compose env var must not override it.) To rename a live station: add or
edit the stationfile and force-recreate — no code deploy, and the compose env var
can stay as a harmless fallback.

## The theming ladder

A theme is a stylesheet. Three levels of effort:

- **Level 0 — no CSS.** `tokens` set CSS custom properties consumed by the base
  sheet. Change the accent or fonts without knowing CSS exists. Token values are
  sanitized (no `<>{};`) so they stay safe single declaration values.
- **Level 1 — a shipped theme.** `theme: "ulm"` (the default) or `theme:
  "classic"`. Served from the package at `/static/themes/<name>.css`.
- **Level 2 — a custom sheet.** Drop `themes/mine.css` in the data dir and set
  `theme: "themes/mine.css"`. Served at `/theme.css`. The path must resolve to a
  real file inside the data dir (no traversal).

Token → CSS custom property:

| token           | property        | notes                                  |
|-----------------|-----------------|----------------------------------------|
| `accent`        | `--accent`      | the one accent color (verified-green)  |
| `font_body`     | `--font-body`   | body font stack                        |
| `font_mono`     | `--font-mono`   | crypto-artifact mono stack             |
| `default_theme` | (data-theme)    | `light` \| `dark` — sets `<html data-theme>` |

## Shipped themes

- **ulm** — the opinionated default. Wikipedia information density × Dieter
  Rams / Vitsoe utilitarian functionalism (brief-20260522-0gjz): high-contrast
  black-on-white, type-driven, restrained, generous whitespace, monospace only
  as a crypto-artifact marker, grayscale + one accent (green=verified,
  amber=wrapped, red=invalid). Color never does structural work — size and
  weight do.
- **classic** — the plain document-web floor. Near-classless, browser-default-ish,
  maximally compatible: "it just works in any reader."

## The theming contract — stable hooks

Themes target these class hooks and semantic elements; **the base markup will not
rename them.** A theme is authored against this contract plus the CSS custom
properties on `:root` (see `skein/web/static/base.css`).

- **Source order is content-first.** The folio *body* precedes provenance,
  metadata, and the cross-reference nav in the DOM (a screen-reader requirement
  and the structural guarantee the spine relies on). A theme may visually float
  the trailing `<aside>` up; it never reorders the DOM.
- **Semantic elements:** `article`, `nav`, `aside`, `header`, `footer`,
  `section`, `dl`/`dt`/`dd`, `details`/`summary`.
- **Named block hooks:**
  - `.station-header` `.station-name` `.station-nav` `.station-footer`
  - `.skein-page` `.skein-folio` `.folio-body` `.prose`
  - `.provenance` + state modifiers `--verified` / `--invalid` / `--unverified`
    / `--unsigned`
  - `.metadata` `.type-tag` `.cryptography` (the monospace crypto-artifact marker)
  - `.threads` `.threads-out` `.threads-in` `.lineage` `.breadcrumb`
  - `.toc` `.entry-list` `.entry` `.site-list` `.section-heading`
  - `.search-form` `.visually-hidden`

The structured read path (`.json`/`.md` and the agent markdown) is unaffected by
any of this — themes restyle the human HTML only.
