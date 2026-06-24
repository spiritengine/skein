"""Tests for the ``skein import`` / ``verify`` CLI verbs (Click CliRunner).

Builds a synthetic legacy SKEIN project on disk in the real layout a cutover
sees — ``PROJECT_ROOT/.skein/data/skein.db`` plus ``PROJECT_ROOT/.skein/data/
sites/<slug>/metadata.json`` — then exercises:

- ``import PROJECT_ROOT --verify`` succeeds, prints FIDELITY OK, and the target
  ``.skein`` then serves the imported folios;
- the fidelity gate exits non-zero on a REAL discrepancy through the real
  counting paths — a genuine hash collision, and a sites dir that drops every
  site — plus a pure-logic check of the dropped-actor tripwire;
- the store-reconciliation invariants hold on a clean import and match the
  destination store's own counts;
- re-importing into the same data dir is idempotent;
- path derivation, explicit overrides, and the missing-source error.

The legacy schema + corpus are reused from ``test_bridge`` so the fixture mirrors
exactly what the bridge reads.
"""

import pytest
from click.testing import CliRunner

from skein.bridge import ImportReport, import_project
from skein.cli import _fidelity_failures, _StoreCounts, cli
from skein.store import SkeinNextStore
from skein.tests.test_bridge import (
    FOLIOS,
    SITES,
    THREADS,
    make_legacy_db,
    make_sites_dir,
)


def _write_project(root, folios, sites):
    """Build a legacy project dir P/.skein/data/{skein.db, sites/} and return P."""
    data = root / ".skein" / "data"
    data.mkdir(parents=True)
    make_legacy_db(data / "skein.db", folios, [])
    make_sites_dir(data, sites)  # creates P/.skein/data/sites/ (empty if sites==[])
    return root


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def project_root(tmp_path):
    """A legacy project dir: P/.skein/data/skein.db + P/.skein/data/sites/."""
    root = tmp_path / "legacyproj"
    data = root / ".skein" / "data"
    data.mkdir(parents=True)
    make_legacy_db(data / "skein.db", FOLIOS, THREADS)
    make_sites_dir(data, SITES)  # -> P/.skein/data/sites/<slug>/metadata.json
    return root


@pytest.fixture
def target(tmp_path):
    return str(tmp_path / ".skein")


# --- happy path: import, verify, then serve the imported data ---------------


def test_import_verify_passes_and_serves_folios(runner, project_root, target):
    r = runner.invoke(
        cli, ["--data-dir", target, "import", str(project_root), "--verify"]
    )
    assert r.exit_code == 0, r.output
    assert "FIDELITY OK" in r.output

    # The target store now resolves the imported legacy ids to content hashes.
    with SkeinNextStore(target) as store:
        h = store.resolve_alias("brief-20260101-aaaa")
        assert h is not None and h.startswith("sha256::")
        assert store.get_folio(h)["title"] == "First brief"

    # And the read verb serves it via the legacy-id alias.
    r2 = runner.invoke(cli, ["--data-dir", target, "folio", "brief-20260101-aaaa"])
    assert r2.exit_code == 0, r2.output
    assert "First brief" in r2.output


def test_import_surfaces_expected_non_failing_counts(runner, project_root, target):
    """The expected (non-gated) outcomes are printed, not silent."""
    r = runner.invoke(cli, ["--data-dir", target, "import", str(project_root)])
    assert r.exit_code == 0, r.output
    # succession->supersedes, unresolved-ref breakdown, and actor folds all show.
    assert "succession renamed to supersedes: 1" in r.output
    assert (
        "unresolved refs kept as legacy ids: 2 occurrences "
        "(2 distinct; cross-project 1, dangling 1)" in r.output
    )
    assert "actor endpoints folded to weaver (lossless): 3" in r.output
    # without --verify there is no fidelity verdict line
    assert "FIDELITY" not in r.output


# --- fidelity gate fails on REAL discrepancies (real counting paths) --------


# Two legacy folios with DIFFERENT folio_ids but identical canonical fields
# (type/title/content/created_at/created_by) hash to the same content address, so
# the second create_folio is IGNOREd: a genuine collision through the real path.
_COLLIDING_FOLIOS = [
    dict(folio_id="brief-20260101-aaaa", type="brief", site_id=None,
         created_at="2026-01-01T10:00:00.111111+00:00", created_by="goblin",
         title="same title", content="same body", status="open",
         content_hash=None),
    dict(folio_id="brief-20260101-bbbb", type="brief", site_id=None,
         created_at="2026-01-01T10:00:00.111111+00:00", created_by="goblin",
         title="same title", content="same body", status="open",
         content_hash=None),
]


def test_verify_flag_fails_on_real_hash_collision(runner, tmp_path, target):
    """Two distinct legacy folios that hash identically trip the gate for real.

    No monkeypatch: the real folio loop sets folio_hash_collisions=1 and the
    store ends up with one fewer folio than carried, so both the collision
    tripwire and the store-reconciliation invariant fire."""
    root = _write_project(tmp_path / "collide", _COLLIDING_FOLIOS, [])
    r = runner.invoke(
        cli, ["--data-dir", target, "import", str(root), "--verify"]
    )
    assert r.exit_code != 0, r.output
    assert "FIDELITY FAILED" in r.output
    assert "folio_hash_collisions == 0" in r.output
    # and the store reconciliation caught the dropped row independently
    assert "count_folios() == folios_carried + sites_carried" in r.output
    # the report itself recorded the real collision
    with SkeinNextStore(target) as store:
        report = import_project(
            str(root / ".skein" / "data" / "skein.db"),
            str(root / ".skein" / "data" / "sites"),
            store,
        )
    assert report.folio_hash_collisions == 1
    assert report.folios_carried == 2


def test_import_fails_when_derived_sites_dir_missing(runner, tmp_path, target):
    """Finding 1(a): a DB present but no derived sites dir errors before import.

    Folios reference sites; a missing sites dir would silently drop every site
    and membership edge, so a derived path that does not exist is a hard error."""
    root = tmp_path / "nosites"
    data = root / ".skein" / "data"
    data.mkdir(parents=True)
    make_legacy_db(data / "skein.db", FOLIOS, THREADS)  # FOLIOS carry site_ids
    # deliberately do NOT create P/.skein/data/sites/
    r = runner.invoke(
        cli, ["--data-dir", target, "import", str(root), "--verify"]
    )
    assert r.exit_code != 0, r.output
    assert "no legacy sites dir" in r.output


def test_verify_fails_when_sites_dir_empty_but_folios_reference_sites(
    runner, tmp_path, target
):
    """Finding 1(b): an empty sites dir (so sites_seen==0) while folios carry a
    site_id trips the gate rather than passing 0==0 silently."""
    root = _write_project(tmp_path / "emptysites", FOLIOS, [])  # sites dir, no JSON
    r = runner.invoke(
        cli, ["--data-dir", target, "import", str(root), "--verify"]
    )
    assert r.exit_code != 0, r.output
    assert "FIDELITY FAILED" in r.output
    assert "sites imported when folios reference them" in r.output


def test_dropped_actor_tripwire_trips_the_gate():
    """Finding 5: the dropped-actor invariant is design-guaranteed never to fire
    on real data, so exercise the gate logic directly (no monkeypatch of the
    importer) with a report that reconciles cleanly except for one dropped actor."""
    report = ImportReport()
    report.folios_seen = report.folios_carried = 1
    report.sites_seen = report.sites_carried = 0
    report.threads_carried = 0
    report.within_threads = 0
    report.actor_endpoints_dropped = 1
    report.dropped_examples = ["agent-x relationship lost"]
    counts = _StoreCounts(folios=1, threads=0, aliases=1)
    failures = _fidelity_failures(report, counts)
    names = [inv for inv, _ in failures]
    # only the dropped-actor tripwire fires; reconciliation is clean
    assert "actor_endpoints_dropped == 0" in names
    assert "count_folios() == folios_carried + sites_carried" not in names


def test_verify_command_passes_on_clean_import(runner, project_root, target):
    r = runner.invoke(cli, ["--data-dir", target, "verify", str(project_root)])
    assert r.exit_code == 0, r.output
    assert "FIDELITY OK" in r.output


# --- store-reconciliation invariants on a clean import ----------------------


def test_reconciliation_invariants_hold_on_clean_import(project_root, target):
    """The three store-reconciliation equalities hold against real store counts."""
    db = str(project_root / ".skein" / "data" / "skein.db")
    sd = str(project_root / ".skein" / "data" / "sites")
    with SkeinNextStore(target) as store:
        report = import_project(db, sd, store)
        # finding 2/3: reconcile the report against what was actually written
        assert store.count_folios() == report.folios_carried + report.sites_carried
        assert store.count_threads() == report.threads_carried + report.within_threads
        assert store.count_aliases() == report.folios_carried
        # concrete: 3 folios + 2 sites; 6 thread rows + 3 within edges
        assert store.count_folios() == 5
        assert store.count_threads() == 9
        # and the gate sees no failures
        counts = _StoreCounts(
            folios=store.count_folios(),
            threads=store.count_threads(),
            aliases=store.count_aliases(),
        )
        assert _fidelity_failures(report, counts) == []


def test_clean_import_qualified_verdict_surfaces_non_gated_loss(
    runner, project_root, target
):
    """Finding 4: the corpus has one closed folio with no status thread, a
    non-gated loss — the passing verdict must qualify, not bare-claim OK."""
    r = runner.invoke(
        cli, ["--data-dir", target, "import", str(project_root), "--verify"]
    )
    assert r.exit_code == 0, r.output
    assert "FIDELITY OK (gated invariants pass; surfaced non-gated losses:" in r.output
    assert "status_without_thread=1" in r.output


# --- idempotency ------------------------------------------------------------


def test_import_twice_is_idempotent(runner, project_root, target):
    r1 = runner.invoke(cli, ["--data-dir", target, "import", str(project_root)])
    assert r1.exit_code == 0, r1.output
    with SkeinNextStore(target) as store:
        first = (store.count_folios(), store.count_threads(), store.list_slugs())

    r2 = runner.invoke(cli, ["--data-dir", target, "import", str(project_root)])
    assert r2.exit_code == 0, r2.output
    with SkeinNextStore(target) as store:
        second = (store.count_folios(), store.count_threads(), store.list_slugs())

    assert first == second


# --- path derivation / overrides / errors -----------------------------------


def test_import_requires_root_or_overrides(runner, target):
    r = runner.invoke(cli, ["--data-dir", target, "import"])
    assert r.exit_code != 0
    assert "give a PROJECT_ROOT" in r.output


def test_import_with_explicit_overrides(runner, project_root, target):
    db = str(project_root / ".skein" / "data" / "skein.db")
    sd = str(project_root / ".skein" / "data" / "sites")
    r = runner.invoke(
        cli,
        ["--data-dir", target, "import", "--legacy-db", db, "--sites-dir", sd,
         "--verify"],
    )
    assert r.exit_code == 0, r.output
    assert "FIDELITY OK" in r.output


def test_import_missing_db_errors(runner, tmp_path, target):
    r = runner.invoke(
        cli, ["--data-dir", target, "import", str(tmp_path / "nonexistent")]
    )
    assert r.exit_code != 0
    assert "no legacy database" in r.output
