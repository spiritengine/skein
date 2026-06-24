"""Tests for the slice-3 web read surface: HTML rendered FROM the wire envelope,
the stationfile wiring, and the theming substrate.

The legacy ContentHashAdapter is retired; HTML and the machine wire are now built
from the one envelope, so the interesting properties to pin are: the two surfaces
agree (no divergence), the page is content-first in the DOM, the stationfile
drives identity + theme (with fail-loud on an unnamed station), and the token ->
CSS-variable path reaches the page.
"""

import json

import pytest
from fastapi.testclient import TestClient

from skein.station import Station
from skein.stationfile import StationfileError
from skein.web.app import (
    ENV_DATA_DIR,
    ENV_PROJECT,
    create_app,
    verdict_state,
)


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / ".skein"


@pytest.fixture
def seeded(data_dir):
    """A station with two linked folios, a status thread, and a sub-site."""
    with Station(data_dir) as st:
        st.create_site("proj", purpose="the project")
        a = st.post(type="finding", site="proj", title="Finding A", content="body A here",
                    created_by="alice", created_at="2026-01-01T00:00:00Z")
        b = st.post(type="brief", site="proj", title="Brief B", content="body B here",
                    created_by="bob", created_at="2026-01-02T00:00:00Z")
        st.store.save_thread(from_id=a, to_id=b, type="reference",
                             created_at="2026-01-03T00:00:00Z")
        st.store.save_thread(to_id=b, type="status", content="closed",
                             created_at="2026-01-04T00:00:00Z")
    return {"data_dir": data_dir, "a": a, "b": b}


def _write_stationfile(data_dir, obj):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "stationfile.json").write_text(json.dumps(obj), encoding="utf-8")


def _make_client(data_dir, monkeypatch, *, stationfile=None, env_project=None):
    monkeypatch.setenv(ENV_DATA_DIR, str(data_dir))
    if env_project is None:
        monkeypatch.delenv(ENV_PROJECT, raising=False)
    else:
        monkeypatch.setenv(ENV_PROJECT, env_project)
    if stationfile is not None:
        _write_stationfile(data_dir, stationfile)
    return TestClient(create_app())


@pytest.fixture
def client(seeded, monkeypatch):
    return _make_client(seeded["data_dir"], monkeypatch, stationfile={"name": "Field Notes"})


# --- stationfile wiring / fail-loud -----------------------------------------


def test_unnamed_station_refuses_to_start(seeded, monkeypatch):
    # No stationfile and no SKEIN_PROJECT bootstrap -> create_app fails loud.
    monkeypatch.setenv(ENV_DATA_DIR, str(seeded["data_dir"]))
    monkeypatch.delenv(ENV_PROJECT, raising=False)
    with pytest.raises(StationfileError):
        create_app()


def test_env_project_bootstraps_name(seeded, monkeypatch):
    client = _make_client(seeded["data_dir"], monkeypatch, env_project="interskein")
    r = client.get("/")
    assert r.status_code == 200 and "interskein" in r.text


def test_stationfile_name_wins_over_env(seeded, monkeypatch):
    client = _make_client(
        seeded["data_dir"], monkeypatch,
        stationfile={"name": "Field Notes"}, env_project="interskein",
    )
    r = client.get("/")
    assert "Field Notes" in r.text and "interskein" not in r.text


def test_tagline_renders(seeded, monkeypatch):
    client = _make_client(
        seeded["data_dir"], monkeypatch,
        stationfile={"name": "Field Notes", "tagline": "notes from the mesh"},
    )
    assert "notes from the mesh" in client.get("/").text


# --- index / site / search HTML ---------------------------------------------


def test_index_lists_sites_and_recent(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "proj" in r.text
    assert "Finding A" in r.text and "Brief B" in r.text


def test_site_detail_and_type_filter(client):
    r = client.get("/site/proj")
    assert r.status_code == 200 and "Finding A" in r.text and "Brief B" in r.text
    r = client.get("/site/proj", params={"type": "finding"})
    assert "Finding A" in r.text and "Brief B" not in r.text


def test_site_404_is_themed_error(client):
    r = client.get("/site/ghost")
    assert r.status_code == 404
    assert "Not resolved" in r.text  # the themed error page, not a bare JSON detail


def test_search_route(client):
    r = client.get("/search", params={"q": "body"})
    assert r.status_code == 200
    assert "Finding A" in r.text and "Brief B" in r.text
    r = client.get("/search", params={"q": "no-such-text-anywhere"})
    assert r.status_code == 200 and "No matches" in r.text


# --- folio HTML from the envelope -------------------------------------------


def test_folio_html_from_wire(client, seeded):
    r = client.get(f"/folio/{seeded['a']}")
    assert r.status_code == 200
    assert "Finding A" in r.text          # title from env.body.title
    assert "body A here" in r.text        # rendered markdown body
    assert seeded["a"] in r.text          # the content hash (provenance)
    assert "UNSIGNED" in r.text           # the verdict line
    assert "provenance--unsigned" in r.text
    assert "Brief B" in r.text            # threads_out peer title


def test_folio_for_agents_box(client, seeded):
    # The handoff widget: a content-addressed .md link + Copy button, the address,
    # the mesh-fetch command, and the unsigned provenance line — all from the wire.
    r = client.get(f"/folio/{seeded['a']}").text
    assert 'class="for-agents"' in r
    assert "For your agent" in r
    # the link is absolute (request-origin fallback in tests), not relative
    assert f"http://testserver/folio/{seeded['a']}.md" in r
    assert "copy-link" in r and 'id="agent-copy"' in r
    assert f"mesh fetch {seeded['a']}" in r
    assert "Unsigned — operator-vouched." in r
    # the clipboard preamble orients a cold agent, and is hidden from assistive tech
    assert "Read this SKEIN folio as Markdown" in r
    assert 'id="agent-copy" class="visually-hidden" aria-hidden="true"' in r


def _cover_folio(data_dir, content_hash, *, subject="alice@example.com",
                 issuer="https://idp", bind=True):
    """Cover a folio with a manifest (+ optional binding) — the unified model's
    replacement for the dissolved per-folio signature sidecar."""
    import json

    from skein import signing
    from skein import profile, sign as sign_mod
    from skein.canon import manifest_descriptor_canonical_bytes
    from skein.identity import content_hash_for_bytes
    from skein.store import SkeinStore

    def signer(cb):
        preimage = profile.profiled_preimage(profile.CANON_PROFILE_MANIFEST_V1, cb)
        bundle = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1", bundles=["x"],
            canonical_bytes=preimage, canon_version=profile.CANON_PROFILE_MANIFEST_V1,
        )
        return sign_mod.SignedResult(bundle=bundle, issuer=issuer, subject=subject)

    ms = sign_mod.sign_manifest([content_hash], signer)
    d = ms["descriptor"]
    mh = content_hash_for_bytes(manifest_descriptor_canonical_bytes(d["root"], d["leaf_count"]))
    store = SkeinStore(data_dir, check_same_thread=False)
    with store.transaction():
        store.add_manifest(d["root"], mh, json.dumps(d, sort_keys=True),
                           json.dumps(ms["leaf_list"]), ms["signature_bundle"],
                           issuer, subject, d["leaf_count"])
        store.add_constituent_attribution(content_hash, "folio", d["root"], issuer, subject)
    if bind:
        store.add_binding(issuer, subject, role="author")
    store.close()


def test_folio_for_agents_box_shows_signer_when_signed(seeded, monkeypatch):
    from skein import signing

    client = _make_client(seeded["data_dir"], monkeypatch, stationfile={"name": "X"})
    _cover_folio(seeded["data_dir"], seeded["a"], subject="alice@example.com", bind=True)
    monkeypatch.setattr(
        signing, "verify_multi",
        lambda cb, b: signing.MultiVerifyResult(
            results=[signing.VerifyResult(status=signing.VerifyStatus.VERIFIED,
                                          issuer="https://idp", subject="alice@example.com")],
            overall=signing.VerifyStatus.VERIFIED,
        ),
    )
    r = client.get(f"/folio/{seeded['a']}").text
    assert "Signed by alice@example.com" in r
    assert "Unsigned — operator-vouched." not in r


def test_folio_for_agents_box_does_not_call_bad_signature_unsigned(seeded, monkeypatch):
    # A folio whose covering manifest's signature is INVALID must NOT read "Unsigned
    # — operator-vouched" in the handoff box; it has a signature, just not a verified
    # one. Understating that in the agent-handoff affordance is the wrong default.
    from skein import signing

    client = _make_client(seeded["data_dir"], monkeypatch, stationfile={"name": "X"})
    _cover_folio(seeded["data_dir"], seeded["a"], bind=True)
    monkeypatch.setattr(
        signing, "verify_multi",
        lambda cb, b: signing.MultiVerifyResult(
            results=[signing.VerifyResult(status=signing.VerifyStatus.SIGNATURE_MISMATCH)],
            overall=signing.VerifyStatus.SIGNATURE_MISMATCH,
        ),
    )
    r = client.get(f"/folio/{seeded['a']}").text
    assert "Signature present but not verified" in r
    assert "Unsigned — operator-vouched." not in r


def test_folio_threads_in_and_out(client, seeded):
    # b is referenced BY a — the incoming edge must surface as "Referenced by".
    r = client.get(f"/folio/{seeded['b']}")
    assert "Referenced by" in r.text
    assert "Finding A" in r.text


def test_skip_link_and_main_landmark(client, seeded):
    # Every page is skip-linkable: a skip-link first in source, landing on #main.
    for path in ("/", f"/folio/{seeded['a']}", "/search?q=body", "/site/proj"):
        r = client.get(path).text
        assert '<a class="skip-link" href="#main">' in r
        assert 'id="main"' in r


def test_folio_head_advertises_wire_alternates(client, seeded):
    r = client.get(f"/folio/{seeded['a']}").text
    head = r[: r.index("</head>")]
    assert f'rel="alternate" type="text/markdown" href="/folio/{seeded["a"]}.md"' in head
    assert f'rel="alternate" type="application/json" href="/folio/{seeded["a"]}.json"' in head


def test_token_palettes_layered_so_operator_override_wins(seeded, monkeypatch):
    # The OS-dark rule (:root:not([data-theme])) out-specifies a plain inline
    # :root, so without @layer an operator's accent would revert under OS dark.
    # base.css layers its token palettes; the inline override stays unlayered and
    # so wins regardless of specificity.
    client = _make_client(
        seeded["data_dir"], monkeypatch,
        stationfile={"name": "X", "tokens": {"accent": "#ff0000"}},
    )
    css = TestClient(create_app()).get("/static/base.css").text
    # the token palettes (incl. the OS-dark rule) live in the layer
    assert "@layer skein-tokens {" in css
    assert ":root:not([data-theme]) {" in css
    page = client.get("/").text
    # the inline operator override is a plain, unlayered :root rule (so it wins)
    assert "<style>:root { --accent: #ff0000;" in page
    assert "@layer" not in page


def test_station_logo_renders_when_configured(seeded, monkeypatch):
    client = _make_client(
        seeded["data_dir"], monkeypatch,
        stationfile={"name": "X", "logo": "/static/logo.svg"},
    )
    r = client.get("/").text
    assert '<img class="station-logo" src="/static/logo.svg"' in r


def test_folio_provenance_is_one_details(client, seeded):
    # Theming rev 3 O7: provenance is a single native <details> whose <summary>
    # carries the verdict; the crypto detail is the body (no separate inner expander).
    r = client.get(f"/folio/{seeded['a']}").text
    assert '<details class="provenance provenance--unsigned"' in r
    # the verdict word lives in the summary (always read by assistive tech)
    summary = r[r.index('class="provenance'):]
    summary = summary[: summary.index("</summary>")]
    assert "UNSIGNED" in summary
    assert "cryptographic detail" not in r  # the old nested expander is gone


def test_folio_hatnote_and_lineage(seeded, monkeypatch):
    # A superseded folio shows the fork hatnote (newer version) and a lineage nav.
    with Station(seeded["data_dir"]) as st:
        newer = st.post(type="finding", site="proj", title="Finding A v2",
                        content="better", created_by="alice", created_at="2026-02-01T00:00:00Z")
        st.store.save_thread(from_id=newer, to_id=seeded["a"], type="supersedes",
                             created_at="2026-02-02T00:00:00Z")
    client = _make_client(seeded["data_dir"], monkeypatch, stationfile={"name": "X"})
    r = client.get(f"/folio/{seeded['a']}").text
    assert 'class="hatnote"' in r and "A newer version of this folio exists" in r
    assert 'class="lineage"' in r and "child (supersedes)" in r
    # the newer folio shows its parent
    r2 = client.get(f"/folio/{newer}").text
    assert "parent (supersedes)" in r2


def test_folio_thread_density_cap(seeded, monkeypatch):
    # More than 8 peers in a group: the first 8 inline, the rest behind an inline
    # "Show all N" <details> (no JS, no navigation); the wire stays uncapped.
    with Station(seeded["data_dir"]) as st:
        for i in range(9):
            p = st.post(type="finding", site="proj", title=f"Peer {i}", content="x",
                        created_by="alice", created_at=f"2026-04-{i + 1:02d}T00:00:00Z")
            st.store.save_thread(from_id=seeded["a"], to_id=p, type="reference",
                                 created_at=f"2026-05-{i + 1:02d}T00:00:00Z")
    client = _make_client(seeded["data_dir"], monkeypatch, stationfile={"name": "X"})
    r = client.get(f"/folio/{seeded['a']}").text
    assert 'class="threads-more"' in r
    assert "Show all 10" in r  # 9 new + the pre-existing reference to Brief B


def test_folio_content_first_source_order(client, seeded):
    # Patrick screen-reader hard req: the folio body precedes the provenance /
    # threads chrome in the DOM.
    r = client.get(f"/folio/{seeded['a']}").text
    body_at = r.index("folio-body")
    aside_at = r.index("folio-meta")
    refs_at = r.index("References")
    assert body_at < aside_at < refs_at


def test_folio_body_script_is_escaped(seeded, monkeypatch):
    # The v0 sanitization posture (markdown html=False): a <script> in a folio
    # body must render escaped, never as live markup. End-to-end regression guard.
    with Station(seeded["data_dir"]) as st:
        x = st.post(type="finding", site="proj", title="XSS probe",
                    content="hello <script>alert('x')</script> world",
                    created_by="eve", created_at="2026-03-01T00:00:00Z")
    client = _make_client(seeded["data_dir"], monkeypatch, stationfile={"name": "X"})
    r = client.get(f"/folio/{x}").text
    assert "<script>alert" not in r
    assert "&lt;script&gt;" in r


def test_folio_body_headings_demoted(seeded, monkeypatch):
    # The folio title is the page's single <h1>; a body that leads with `# Title`
    # must not emit a second <h1> — its headings nest under the title.
    with Station(seeded["data_dir"]) as st:
        h = st.post(type="finding", site="proj", title="Heading test",
                    content="# Body heading\n\nprose\n\n## Sub", created_by="alice",
                    created_at="2026-03-02T00:00:00Z")
    client = _make_client(seeded["data_dir"], monkeypatch, stationfile={"name": "X"})
    r = client.get(f"/folio/{h}").text
    assert r.count("<h1") == 1                 # only the title
    assert "<h2>Body heading</h2>" in r        # body `#` demoted to h2
    assert "<h3>Sub</h3>" in r                  # body `##` demoted to h3


def test_folio_404_is_themed_error(client):
    # A well-formed full digest that resolves to nothing -> not_found, themed.
    r = client.get("/folio/sha256::" + "0" * 64)
    assert r.status_code == 404 and "Not resolved" in r.text


def test_html_and_json_agree_on_status(client, seeded):
    # The whole point of HTML-from-wire: no divergence. b is closed via a status
    # thread; both surfaces must report it (HTML reads the same asserted block).
    html = client.get(f"/folio/{seeded['b']}").text
    env = client.get(f"/folio/{seeded['b']}.json").json()
    assert env["asserted"]["status"] == "closed"
    assert "closed" in html


# --- theming substrate ------------------------------------------------------


def test_base_and_default_theme_linked(client):
    r = client.get("/").text
    assert "/static/base.css" in r
    assert "/static/themes/ulm.css" in r  # ulm is the default


def test_classic_theme_selected(seeded, monkeypatch):
    client = _make_client(
        seeded["data_dir"], monkeypatch,
        stationfile={"name": "X", "theme": "classic"},
    )
    r = client.get("/").text
    assert "/static/themes/classic.css" in r
    assert "/static/themes/ulm.css" not in r


def test_token_accent_reaches_the_page(seeded, monkeypatch):
    client = _make_client(
        seeded["data_dir"], monkeypatch,
        stationfile={"name": "X", "tokens": {"accent": "#123456"}},
    )
    r = client.get("/").text
    assert "--accent: #123456;" in r


def test_font_stack_token_not_html_escaped(seeded, monkeypatch):
    # A quoted font name must reach the <style> block as literal CSS, not
    # &#39;-escaped (which would break font-family). Safe because the loader
    # already stripped the markup-breaking chars.
    client = _make_client(
        seeded["data_dir"], monkeypatch,
        stationfile={"name": "X", "tokens": {"font_body": "Georgia, 'Times New Roman', serif"}},
    )
    r = client.get("/").text
    assert "--font-body: Georgia, 'Times New Roman', serif;" in r


def test_default_theme_token_sets_data_theme(seeded, monkeypatch):
    client = _make_client(
        seeded["data_dir"], monkeypatch,
        stationfile={"name": "X", "tokens": {"default_theme": "dark"}},
    )
    assert 'data-theme="dark"' in client.get("/").text


def test_style_breakout_token_never_reaches_page(seeded, monkeypatch):
    # End-to-end: a token value carrying </style> is dropped at load, so the
    # served page's <style> block can never be escaped by station config.
    client = _make_client(
        seeded["data_dir"], monkeypatch,
        stationfile={"name": "X", "tokens": {"accent": "x</style><script>alert(1)</script>"}},
    )
    r = client.get("/").text
    assert "<script>alert(1)</script>" not in r
    assert "</style><script>" not in r


def test_shipped_theme_static_served(client):
    r = client.get("/static/themes/ulm.css")
    assert r.status_code == 200
    assert "text/css" in r.headers["content-type"]


def test_custom_theme_symlink_escape_blocked(seeded, monkeypatch, tmp_path):
    # The /theme.css route re-checks containment at read time: a sheet swapped for
    # a symlink pointing outside the data dir AFTER startup must not be disclosed.
    themes = seeded["data_dir"] / "themes"
    themes.mkdir(parents=True, exist_ok=True)
    real = themes / "mine.css"
    real.write_text("body { color: rebeccapurple; }", encoding="utf-8")
    client = _make_client(
        seeded["data_dir"], monkeypatch, stationfile={"name": "X", "theme": "themes/mine.css"},
    )
    assert "rebeccapurple" in client.get("/theme.css").text  # legit sheet served

    secret = tmp_path / "outside-secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    real.unlink()
    real.symlink_to(secret)  # swap the sheet for a symlink escaping the data dir
    escaped = client.get("/theme.css")
    assert "TOP SECRET" not in escaped.text  # escape caught, nothing disclosed


def test_custom_theme_served_from_data_dir(seeded, monkeypatch):
    themes = seeded["data_dir"] / "themes"
    themes.mkdir(parents=True, exist_ok=True)
    (themes / "mine.css").write_text("body { color: rebeccapurple; }", encoding="utf-8")
    client = _make_client(
        seeded["data_dir"], monkeypatch,
        stationfile={"name": "X", "theme": "themes/mine.css"},
    )
    page = client.get("/").text
    assert "/theme.css" in page
    css = client.get("/theme.css")
    assert css.status_code == 200 and "rebeccapurple" in css.text


# --- negotiation still routes (the wire is unchanged) -----------------------


def test_machine_wire_still_negotiates(client, seeded):
    # An agent UA / Accept still gets the wire, not HTML.
    r = client.get(f"/folio/{seeded['a']}", headers={"accept": "application/json"})
    assert r.json()["kind"] == "folio"
    r = client.get(f"/folio/{seeded['a']}.md")
    assert "body A here" in r.text


# --- unit -------------------------------------------------------------------


def test_verdict_state_mapping():
    assert verdict_state("SIGNED — alice (verified)") == "verified"
    assert verdict_state("SIGNATURE INVALID — bad sig") == "invalid"
    assert verdict_state("UNVERIFIED — verifier unavailable (X)") == "unverified"
    # NOT VERIFIED — … is a load-bearing not-verified state (manifest verifies but
    # signer unbound/revoked, or membership/proof fails); it gets the "unverified"
    # accent, never the benign never-signed "unsigned" bucket.
    assert verdict_state("NOT VERIFIED — revoked binding") == "unverified"
    assert verdict_state("NOT VERIFIED — unbound signer") == "unverified"
    assert verdict_state("NOT VERIFIED — not in manifest") == "unverified"
    assert verdict_state("NOT VERIFIED — proof missing") == "unverified"
    assert verdict_state("UNSIGNED — operator-vouched") == "unsigned"
    assert verdict_state(None) == "unsigned"


def test_concurrent_requests_isolated_connections(client):
    import concurrent.futures

    def hit(_):
        return client.get("/").status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        codes = list(ex.map(hit, range(40)))
    assert codes == [200] * 40
