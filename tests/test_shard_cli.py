"""CLI tests for SHARD commands."""

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent))

from client.cli import _run_shard_xgun, _run_shard_xgun_in_process, cli


class _MockShardModule:
    class ShardError(Exception):
        pass

    def __init__(self):
        self.cleanup_calls = []

    def cleanup_shard(self, worktree_name, keep_branch=False, caller_cwd=None):
        self.cleanup_calls.append(
            {
                "worktree_name": worktree_name,
                "keep_branch": keep_branch,
                "caller_cwd": caller_cwd,
            }
        )

    def is_graft(self, worktree_name):
        return False


class TestShardReviewCli:
    def test_worktree_name_dispatches_to_inspect(self):
        runner = CliRunner()
        shard_module = MagicMock()
        shard_module.ShardError = _MockShardModule.ShardError
        shard_module.get_shard_status.return_value = None

        with patch("client.cli.get_shard_worktree_module", return_value=shard_module):
            result = runner.invoke(
                cli,
                ["--agent", "reviewer", "shard", "review", "demo-shard"],
            )

        assert result.exit_code != 0
        assert "SHARD not found: demo-shard" in result.output
        shard_module.get_shard_status.assert_called_once_with("demo-shard")
        shard_module.get_review_queue.assert_not_called()

    def test_no_worktree_name_keeps_review_queue_behavior(self):
        runner = CliRunner()
        shard_module = MagicMock()
        shard_module.get_review_queue.return_value = {
            "ready": [],
            "needs_commit": [],
            "conflicts": [],
            "stale": [],
        }

        with patch("client.cli.get_shard_worktree_module", return_value=shard_module):
            result = runner.invoke(cli, ["shard", "review"])

        assert result.exit_code == 0, result.output
        assert result.output == "No SHARDs found\n"
        shard_module.get_review_queue.assert_called_once_with(stale_days=7)
        shard_module.get_shard_status.assert_not_called()


def _xgun_api(resolve_diff, qgun_scan, sgun_sniff, check_did_not_run):
    class FakeArtifactError(Exception):
        pass

    return (
        resolve_diff,
        FakeArtifactError,
        qgun_scan,
        sgun_sniff,
        check_did_not_run,
    )


def _reading(*, passed=True, flags=None, signals=None):
    return SimpleNamespace(
        passed=passed,
        flags=flags or [],
        signals=signals or [],
        stats={"files_changed": 1},
    )


class TestShardXgunScan:
    def test_uses_typed_xgun_api_for_explicit_shard_diff(self):
        resolve_diff = MagicMock(
            return_value=("diff text", ["/repo/changed.py"], {"changed.py": "new content"})
        )
        qgun_scan = MagicMock(
            return_value=_reading(
                passed=False,
                flags=[
                    SimpleNamespace(
                        check="ruff_quick",
                        file="changed.py",
                        line=4,
                        message="undefined name",
                        severity="medium",
                    )
                ],
                signals=[
                    SimpleNamespace(
                        check="size_gate",
                        level=SimpleNamespace(value="yellow"),
                        message="large diff",
                    )
                ],
            )
        )
        sgun_sniff = MagicMock(
            return_value=[
                SimpleNamespace(
                    kind="broad-except",
                    file="/repo/changed.py",
                    line=8,
                    severity=6,
                    reason="broad exception handler",
                )
            ]
        )
        check_did_not_run = MagicMock(return_value=False)

        with patch(
            "client.cli._load_xgun_api",
            return_value=_xgun_api(
                resolve_diff,
                qgun_scan,
                sgun_sniff,
                check_did_not_run,
            ),
        ):
            result = _run_shard_xgun_in_process("/repo", "master")

        resolve_diff.assert_called_once_with("/repo", "master", "HEAD", None)
        qgun_scan.assert_called_once_with("diff text", {"changed.py": "new content"})
        sgun_sniff.assert_called_once_with(["/repo/changed.py"])
        assert result["status"] == "completed"
        assert result["summary"] == {
            "passed": False,
            "checks_failed": [],
            "flags": 1,
            "signals": 1,
            "smells": 1,
        }
        assert result["qgun"]["flags"][0]["severity"] == "medium"
        assert result["timestamp"].endswith("Z")
        assert result["sgun"]["files_checked"] == []

    def test_missing_xgun_is_visible_but_does_not_raise(self):
        with patch(
            "client.cli._load_xgun_api",
            side_effect=ImportError("No module named 'xgun.artifact'"),
        ):
            result = _run_shard_xgun_in_process("/repo", "master")

        assert result["status"] == "unavailable"
        assert "xgun is unavailable" in result["message"]
        assert "Quality reading is incomplete" in result["message"]
        assert "Raise this before merge" in result["message"]

    def test_broken_xgun_module_is_visible_but_does_not_raise(self):
        with patch(
            "client.cli._load_xgun_api",
            side_effect=SyntaxError("invalid syntax", ("xgun/artifact.py", 12, 1, "bad")),
        ):
            result = _run_shard_xgun_in_process("/repo", "master")

        assert result["status"] == "unavailable"
        assert "SyntaxError" in result["message"]
        assert "xgun is unavailable" in result["message"]
        assert "Raise this before merge" in result["message"]

    def test_unresolvable_base_is_visible_and_does_not_scan(self):
        resolve_diff = MagicMock()
        api = _xgun_api(resolve_diff, MagicMock(), MagicMock(), MagicMock())

        with patch("client.cli._load_xgun_api", return_value=api):
            result = _run_shard_xgun_in_process("/repo", None)

        assert result["status"] == "not_run"
        assert "base branch could not be determined" in result["message"]
        assert "Raise this before merge" in result["message"]
        resolve_diff.assert_not_called()

    def test_artifact_error_is_visible_but_does_not_raise(self):
        class FakeArtifactError(Exception):
            pass

        resolve_diff = MagicMock(side_effect=FakeArtifactError("bad ref master"))
        api = (resolve_diff, FakeArtifactError, MagicMock(), MagicMock(), MagicMock())

        with patch("client.cli._load_xgun_api", return_value=api):
            result = _run_shard_xgun_in_process("/repo", "master")

        assert result["status"] == "error"
        assert "could not resolve the shard diff" in result["message"]
        assert "bad ref master" in result["message"]
        assert "Quality reading is incomplete" in result["message"]

    def test_tool_did_not_run_makes_result_incomplete_not_passed(self):
        failed_signal = SimpleNamespace(
            check="ast_grep",
            level=SimpleNamespace(value="red"),
            message="ast-grep exited 8; security scan did not run",
        )
        resolve_diff = MagicMock(return_value=("diff text", [], {}))
        qgun_scan = MagicMock(return_value=_reading(signals=[failed_signal]))
        sgun_sniff = MagicMock()
        check_did_not_run = MagicMock(side_effect=lambda signal: signal is failed_signal)

        with patch(
            "client.cli._load_xgun_api",
            return_value=_xgun_api(
                resolve_diff,
                qgun_scan,
                sgun_sniff,
                check_did_not_run,
            ),
        ):
            result = _run_shard_xgun_in_process("/repo", "master")

        assert result["status"] == "incomplete"
        assert result["summary"]["passed"] is False
        assert result["summary"]["checks_failed"] == ["ast_grep"]
        assert result["qgun"]["signals"][0]["did_not_run"] is True
        assert "Raise this before merge" in result["message"]
        sgun_sniff.assert_not_called()

    def test_unusable_typed_result_is_visible_but_does_not_raise(self):
        signal = SimpleNamespace(
            check="ast_grep",
            level=SimpleNamespace(value="red"),
            message="failed",
        )
        resolve_diff = MagicMock(return_value=("diff text", [], {}))
        qgun_scan = MagicMock(return_value=_reading(signals=[signal]))
        check_did_not_run = MagicMock(side_effect=TypeError("incompatible xgun API"))

        with patch(
            "client.cli._load_xgun_api",
            return_value=_xgun_api(
                resolve_diff,
                qgun_scan,
                MagicMock(),
                check_did_not_run,
            ),
        ):
            result = _run_shard_xgun_in_process("/repo", "master")

        assert result["status"] == "error"
        assert "returned an unusable result" in result["message"]
        assert "incompatible xgun API" in result["message"]
        assert "Raise this before merge" in result["message"]

    def test_owned_worker_returns_typed_result(self):
        expected = {"status": "completed", "summary": {"passed": True}}

        with patch(
            "client.cli._run_shard_xgun_in_process",
            return_value=expected,
        ):
            result = _run_shard_xgun("/repo", "master", timeout=1)

        assert result == expected

    def test_total_timeout_is_visible_and_stops_waiting(self):
        def hang(*_args):
            time.sleep(5)

        started = time.monotonic()
        with patch("client.cli._run_shard_xgun_in_process", side_effect=hang):
            result = _run_shard_xgun("/repo", "master", timeout=0.05)
        elapsed = time.monotonic() - started

        assert elapsed < 2
        assert result["status"] == "error"
        assert "timed out after 0.05 seconds" in result["message"]
        assert "Quality reading is incomplete" in result["message"]


def _make_inspect_shard_module():
    shard_module = MagicMock()
    shard_module.ShardError = _MockShardModule.ShardError
    shard_module.get_shard_status.return_value = {
        "branch_name": "shard-demo-shard",
        "worktree_path": "/repo/worktrees/demo-shard",
    }
    shard_module.get_shard_git_info.return_value = {
        "commits_ahead": 1,
        "uncommitted": [],
        "commit_log": [],
        "diffstat": "",
    }
    shard_module.get_shard_drift_info.return_value = {
        "base_branch": "master",
        "base_commit_short": "abc1234",
        "base_commit_date": "2026-08-01",
        "base_commits_ahead": 0,
        "base_notable_changes": [],
        "conflict_status": "clean",
        "conflict_files": [],
        "is_nested": False,
        "work_diff_stat": None,
    }
    shard_module.is_graft.return_value = False
    return shard_module


class TestShardInspectXgunOutput:
    def test_human_output_tells_agents_to_raise_unavailable_xgun(self):
        runner = CliRunner()
        shard_module = _make_inspect_shard_module()
        unavailable = {
            "status": "unavailable",
            "message": (
                "xgun is unavailable to /python: missing package. "
                "Quality reading is incomplete. Raise this before merge and rerun "
                "`skein shard inspect`."
            ),
        }

        with (
            patch("client.cli.get_shard_worktree_module", return_value=shard_module),
            patch("client.cli.make_request", return_value=[]),
            patch("client.cli._run_shard_xgun", return_value=unavailable),
        ):
            result = runner.invoke(cli, ["shard", "inspect", "demo-shard"])

        assert result.exit_code == 0, result.output
        assert "=== Code Quality (xgun) ===" in result.output
        assert "xgun is unavailable" in result.output
        assert "Quality reading is incomplete" in result.output
        assert "Raise this before merge" in result.output

    def test_human_output_marks_failed_check_and_result_incomplete(self):
        runner = CliRunner()
        shard_module = _make_inspect_shard_module()
        incomplete = {
            "status": "incomplete",
            "message": (
                "1 xgun check(s) could not run: ast_grep. Quality reading is incomplete. "
                "Raise this before merge and rerun `skein shard inspect`."
            ),
            "qgun": {
                "passed": True,
                "flags": [],
                "signals": [
                    {
                        "check": f"green_{index}",
                        "level": "green",
                        "message": "check passed",
                    }
                    for index in range(6)
                ]
                + [
                    {
                        "check": "ast_grep",
                        "level": "red",
                        "message": "ast-grep exited 8; security scan did not run",
                        "did_not_run": True,
                    }
                ],
                "stats": {},
            },
            "sgun": {"smells": []},
            "summary": {
                "passed": False,
                "checks_failed": ["ast_grep"],
                "flags": 0,
                "signals": 7,
                "smells": 0,
            },
        }

        with (
            patch("client.cli.get_shard_worktree_module", return_value=shard_module),
            patch("client.cli.make_request", return_value=[]),
            patch("client.cli._run_shard_xgun", return_value=incomplete),
        ):
            result = runner.invoke(cli, ["shard", "inspect", "demo-shard"])

        assert result.exit_code == 0, result.output
        assert "! Quality: Incomplete" in result.output
        assert "[red did-not-run] [ast_grep]" in result.output
        assert "ast-grep exited 8; security scan did not run" in result.output
        assert "... and 2 more" in result.output
        assert "Raise this before merge" in result.output
        assert "Quality: Passed" not in result.output

    def test_json_always_contains_xgun_status(self):
        runner = CliRunner()
        shard_module = _make_inspect_shard_module()
        unavailable = {
            "status": "unavailable",
            "message": "xgun unavailable; quality reading incomplete",
        }

        with (
            patch("client.cli.get_shard_worktree_module", return_value=shard_module),
            patch("client.cli.make_request", return_value=[]),
            patch("client.cli._run_shard_xgun", return_value=unavailable),
        ):
            result = runner.invoke(cli, ["shard", "inspect", "demo-shard", "--json"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["xgun"] == unavailable


class TestShardCleanupCli:
    def test_cleanup_yes_skips_prompt_and_proceeds(self):
        runner = CliRunner()
        shard_module = _MockShardModule()

        with patch("client.cli.get_shard_worktree_module", return_value=shard_module):
            result = runner.invoke(
                cli,
                ["shard", "cleanup", "demo-shard", "--yes"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "Are you sure you want to cleanup this SHARD?" not in result.output
        assert "✓ Cleaned up SHARD: demo-shard" in result.output
        assert shard_module.cleanup_calls == [
            {
                "worktree_name": "demo-shard",
                "keep_branch": False,
                "caller_cwd": str(Path.cwd()),
            }
        ]

    def test_cleanup_without_yes_aborts_cleanly_when_stdin_is_closed(self):
        runner = CliRunner()
        shard_module = _MockShardModule()

        with patch("client.cli.get_shard_worktree_module", return_value=shard_module):
            result = runner.invoke(cli, ["shard", "cleanup", "demo-shard"], input=None)

        assert result.exit_code == 1
        assert "Are you sure you want to cleanup this SHARD?" in result.output
        assert "Aborted!" in result.output
        assert shard_module.cleanup_calls == []


def _make_where_shard_module(**overrides):
    """Return a mock shard module wired up for `shard where` tests."""
    m = MagicMock()

    class ShardError(Exception):
        pass

    m.ShardError = ShardError
    location = {
        "worktree_name": "demo-shard-20260101-001",
        "worktree_path": "/home/agent/.skein/worktrees/skein-ab12cd34/demo-shard-20260101-001",
        "worktrees_dir": "/home/agent/.skein/worktrees/skein-ab12cd34",
        "project_root": "/home/agent/projects/skein",
        "branch_name": "shard-demo-shard-20260101-001",
        "exists": True,
        "registered": True,
        "source": "git",
    }
    location.update(overrides)
    m.get_shard_location.return_value = location
    return m


class TestShardWhereCli:
    def test_where_prints_path_and_origin_repo(self):
        runner = CliRunner()
        shard_module = _make_where_shard_module()

        with patch("client.cli.get_shard_worktree_module", return_value=shard_module):
            result = runner.invoke(
                cli,
                ["shard", "where", "demo-shard-20260101-001"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "/home/agent/.skein/worktrees/skein-ab12cd34/demo-shard-20260101-001" in (
            result.output
        )
        assert "/home/agent/projects/skein" in result.output
        assert "shard-demo-shard-20260101-001" in result.output
        assert "⚠️" not in result.output

    def test_path_only_prints_a_bare_path(self):
        """WHY: The point of --path-only is `cd "$(skein shard where X --path-only)"`,
        which breaks if anything else is on stdout."""
        runner = CliRunner()
        shard_module = _make_where_shard_module()

        with patch("client.cli.get_shard_worktree_module", return_value=shard_module):
            result = runner.invoke(
                cli,
                ["shard", "where", "demo-shard-20260101-001", "--path-only"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert result.output.strip() == (
            "/home/agent/.skein/worktrees/skein-ab12cd34/demo-shard-20260101-001"
        )

    def test_json_output_is_the_full_location(self):
        import json

        runner = CliRunner()
        shard_module = _make_where_shard_module()

        with patch("client.cli.get_shard_worktree_module", return_value=shard_module):
            result = runner.invoke(
                cli,
                ["shard", "where", "demo-shard-20260101-001", "--json"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["project_root"] == "/home/agent/projects/skein"
        assert payload["source"] == "git"

    def test_missing_worktree_is_flagged(self):
        runner = CliRunner()
        shard_module = _make_where_shard_module(
            exists=False, registered=False, source="expected"
        )

        with patch("client.cli.get_shard_worktree_module", return_value=shard_module):
            result = runner.invoke(
                cli,
                ["shard", "where", "demo-shard-20260101-001"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "does not exist" in result.output
        assert "expected" in result.output

    def test_unregistered_worktree_is_flagged(self):
        """WHY: A directory git no longer tracks is a different problem from a
        missing one, and needs a different fix."""
        runner = CliRunner()
        shard_module = _make_where_shard_module(
            exists=True, registered=False, source="database"
        )

        with patch("client.cli.get_shard_worktree_module", return_value=shard_module):
            result = runner.invoke(
                cli,
                ["shard", "where", "demo-shard-20260101-001"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "does not list it as a worktree" in result.output

    def test_shard_error_becomes_a_clean_cli_error(self):
        runner = CliRunner()
        shard_module = _make_where_shard_module()
        shard_module.get_shard_location.side_effect = shard_module.ShardError(
            "Worktree name is required"
        )

        with patch("client.cli.get_shard_worktree_module", return_value=shard_module):
            result = runner.invoke(cli, ["shard", "where", "x"])

        assert result.exit_code != 0
        assert "Worktree name is required" in result.output


def _make_tender_shard_module(project_name="warp"):
    """Return a mock shard module wired up for tender tests."""
    m = MagicMock()
    m.get_shard_status.return_value = {
        "worktree_path": f"/home/patrick/projects/{project_name}/worktrees/demo-shard-001",
    }
    m.get_tender_metadata.return_value = {
        "last_commit_message": "Add feature X",
        "files_modified": ["foo.py"],
        "commits": 1,
        "branch_name": "shard-demo-shard-001",
        "name": "demo-shard-001",
    }
    return m


def _make_request_dispatcher(available_site_ids, folio_result=None):
    """Return a make_request mock that serves /sites and /folios."""
    sites_payload = [{"site_id": sid} for sid in available_site_ids]
    folio_payload = folio_result or {"folio_id": "tender-20260101-test1"}

    def dispatch(method, endpoint, base_url, agent_id, **kwargs):
        if method == "GET" and endpoint == "/sites":
            return sites_payload
        if method == "POST" and endpoint == "/folios":
            return folio_payload
        return {}

    return dispatch


class TestShardTenderSiteValidation:
    def test_derived_site_missing_no_fallback_errors_with_available_list(self):
        """When warp-development and shard-review both absent, error with available sites."""
        runner = CliRunner()
        shard_module = _make_tender_shard_module("warp")
        dispatch = _make_request_dispatcher(["build", "portfolio-theses"])

        with (
            patch("client.cli.get_shard_worktree_module", return_value=shard_module),
            patch("client.cli.make_request", side_effect=dispatch),
        ):
            result = runner.invoke(
                cli, ["shard", "tender", "demo-shard-001"], catch_exceptions=False
            )

        assert result.exit_code != 0
        assert "warp-development" in result.output
        assert "shard-review" in result.output
        assert "build" in result.output
        assert "portfolio-theses" in result.output
        assert "--site" in result.output

    def test_derived_site_missing_falls_through_to_shard_review(self):
        """When warp-development is absent but shard-review exists, use shard-review."""
        runner = CliRunner()
        shard_module = _make_tender_shard_module("warp")
        dispatch = _make_request_dispatcher(["build", "shard-review"])

        with (
            patch("client.cli.get_shard_worktree_module", return_value=shard_module),
            patch("client.cli.make_request", side_effect=dispatch),
        ):
            result = runner.invoke(
                cli, ["shard", "tender", "demo-shard-001"], catch_exceptions=False
            )

        assert result.exit_code == 0, result.output
        assert "shard-review" in result.output

    def test_derived_site_exists_is_used(self):
        """When skein-development exists, it is used without error."""
        runner = CliRunner()
        shard_module = _make_tender_shard_module("skein")
        dispatch = _make_request_dispatcher(["skein-development", "skein-dev"])

        with (
            patch("client.cli.get_shard_worktree_module", return_value=shard_module),
            patch("client.cli.make_request", side_effect=dispatch),
        ):
            result = runner.invoke(
                cli, ["shard", "tender", "demo-shard-001"], catch_exceptions=False
            )

        assert result.exit_code == 0, result.output
        assert "skein-development" in result.output

    def test_explicit_site_missing_errors_with_available_list(self):
        """When --site names a non-existent site, error with available sites."""
        runner = CliRunner()
        shard_module = _make_tender_shard_module("warp")
        dispatch = _make_request_dispatcher(["build", "portfolio-theses"])

        with (
            patch("client.cli.get_shard_worktree_module", return_value=shard_module),
            patch("client.cli.make_request", side_effect=dispatch),
        ):
            result = runner.invoke(
                cli,
                ["shard", "tender", "demo-shard-001", "--site", "nonexistent-site"],
                catch_exceptions=False,
            )

        assert result.exit_code != 0
        assert "nonexistent-site" in result.output
        assert "build" in result.output
        assert "portfolio-theses" in result.output

    def test_explicit_site_valid_succeeds(self):
        """When --site names an existing site, tender proceeds normally."""
        runner = CliRunner()
        shard_module = _make_tender_shard_module("warp")
        dispatch = _make_request_dispatcher(["build", "portfolio-theses"])

        with (
            patch("client.cli.get_shard_worktree_module", return_value=shard_module),
            patch("client.cli.make_request", side_effect=dispatch),
        ):
            result = runner.invoke(
                cli,
                ["shard", "tender", "demo-shard-001", "--site", "build"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "build" in result.output
