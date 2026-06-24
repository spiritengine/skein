"""Shard command group for the content-hash CLI (agent-coordination port, Stage 1).

The shard *engine* (:mod:`skein.shard`) is pure local git + a
``.skein/shards.db`` sidecar and is unchanged from legacy. What changes here is
the CLI layer: where legacy ``client/cli.py`` reached a running SKEIN server over
HTTP for the handful of SKEIN-side bookkeeping touchpoints (tender folios, the
spawn tracking thread, pause/resume replies), these verbs now talk to the local
content-hash :class:`~skein.station.Station` directly — no server.

The rewired touchpoints:

- ``tender`` / merge's auto-tender: POST /folios → :meth:`Station.post`, with the
  tender's structured fields carried in the folio content as a pinned JSON
  envelope (:mod:`skein.tender`) since the ``folios`` table has no metadata
  column.
- ``triage`` / ``inspect``: GET /folios?type=tender → :meth:`Station.folios_by_type`,
  then newest-per-worktree selection (the legacy server-iteration-order map was
  not newest-wins).
- ``spawn``: POST /threads (tag) → a local ``tag`` thread.
- ``pause`` / ``resume``: GET /threads + POST replies → local ``tag`` thread
  lookup + a ``reply`` thread.

The purely-git verbs (``list``, ``show``, ``diff``, ``cleanup``, ``graft``,
``review``, ``stash``, ``apply``, ``test``) carry over unchanged except for
importing the copied engine and the ``apply``/``test`` ``PROJECT_ROOT`` fix.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import click

from . import shard as shard_engine
from .station import Station, UnknownSite
from .tender import (
    latest_tenders_by_worktree,
    render_tender_content,
)

ENV_AGENT = "SKEIN_AGENT"


# --- helpers ----------------------------------------------------------------


def _open_station(ctx: click.Context) -> Station:
    return Station(ctx.obj.get("data_dir") if ctx.obj else None)


def _default_author() -> Optional[str]:
    return os.environ.get(ENV_AGENT) or os.environ.get("SKEIN_AGENT")


def make_title_from_content(content: str, max_length: int = 100) -> str:
    """Generate a clean title from content: strip markdown, normalize, truncate."""
    title = content.split("\n")[0].strip()
    title = re.sub(r"^#+\s*", "", title)
    title = re.sub(r"\*\*(.+?)\*\*", r"\1", title)
    title = re.sub(r"__(.+?)__", r"\1", title)
    title = re.sub(r"\*(.+?)\*", r"\1", title)
    title = re.sub(r"_([^_]+)_", r"\1", title)
    title = re.sub(r"`([^`]+)`", r"\1", title)
    title = re.sub(r"~~(.+?)~~", r"\1", title)
    title = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", title)
    title = " ".join(title.split())
    if len(title) > max_length:
        title = title[: max_length - 3].rstrip() + "..."
    return title


def load_rites_config(project_root: Path) -> Dict[str, Any]:
    """Load rites configuration from ``<project_root>/.skein/rites.yaml``."""
    rites_file = project_root / ".skein" / "rites.yaml"
    if not rites_file.exists():
        return {"rites": {}}
    try:
        import yaml

        with open(rites_file) as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        raise click.ClickException("PyYAML required for rites. Run: pip install pyyaml")
    except Exception as e:
        raise click.ClickException(f"Failed to load rites config: {e}")


def run_rite_commands(
    rite_name: str,
    rite_config: Dict[str, Any],
    working_dir: Optional[Path] = None,
    verbose: bool = False,
) -> bool:
    """Execute a rite's commands; return True iff all succeeded."""
    commands = rite_config.get("commands", [])
    if not commands:
        click.echo(f"Rite '{rite_name}' has no commands defined", err=True)
        return False
    if isinstance(commands, str):
        commands = [commands]
    cwd = str(working_dir) if working_dir else None
    for i, cmd in enumerate(commands, 1):
        if verbose:
            click.echo(f"[{i}/{len(commands)}] {cmd}")
        try:
            result = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=not verbose, text=True)
            if result.returncode != 0:
                if not verbose and result.stderr:
                    click.echo(result.stderr, err=True)
                if not verbose and result.stdout:
                    click.echo(result.stdout)
                click.echo(f"✗ Command failed (exit {result.returncode}): {cmd}", err=True)
                return False
        except Exception as e:
            click.echo(f"✗ Failed to run command: {e}", err=True)
            return False
    return True


# --- group ------------------------------------------------------------------


@click.group()
@click.option(
    "--project",
    "project_path",
    help="Path to project (default: SKEIN_PROJECT env or current directory)",
)
@click.pass_context
def shard(ctx: click.Context, project_path: Optional[str]) -> None:
    """SHARD agent coordination - worktree management for parallel agent work."""
    ctx.ensure_object(dict)
    effective_project = project_path or os.environ.get("SKEIN_PROJECT")
    if effective_project:
        try:
            shard_engine.set_project_root(effective_project)
        except shard_engine.ShardError as e:
            raise click.ClickException(str(e))


@click.command(name="shards", hidden=True)
@click.option("--active", is_flag=True, help="Show only active SHARDs")
@click.option("--agent", "filter_agent", help="Filter by shard name")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def shards_shortcut(ctx, active, filter_agent, output_json):
    """Shortcut for 'skein shard list'."""
    ctx.invoke(shard_list, active=active, filter_agent=filter_agent, output_json=output_json)


@shard.command("spawn")
@click.option("--agent", "spawn_agent", required=True, help="Agent ID for this SHARD")
@click.option("--brief", help="Brief ID this SHARD relates to")
@click.option("--description", help="Work description")
@click.option(
    "--base",
    "base_branch",
    default=None,
    help="Branch to fork from (default: auto-detected from repo)",
)
@click.pass_context
def shard_spawn(ctx, spawn_agent, brief, description, base_branch):
    """
    Spawn a new SHARD: create git branch + worktree for isolated agent work.

    Example:
        skein shard spawn --agent opus-security-architect --brief brief-123 --description "Bash security"
        skein shard spawn --agent opus --base feature-branch
    """
    try:
        if base_branch is None:
            base_branch = shard_engine._detect_default_branch()

        shard_info = shard_engine.spawn_shard(
            name=spawn_agent,
            brief_id=brief,
            description=description,
            base_branch=base_branch,
        )

        # Track this SHARD with a local "tag" thread carrying SHARD metadata in
        # content (the same shape legacy posted to the server).
        thread_content = json.dumps(
            {
                "tag": "shard",
                "shard_id": shard_info["shard_id"],
                "worktree_name": shard_info["worktree_name"],
                "worktree_path": shard_info["worktree_path"],
                "branch_name": shard_info["branch_name"],
                "status": "spawned",
                "description": description or "",
            }
        )
        try:
            with _open_station(ctx) as station:
                # from_id is the shard's own agent (the subject), but the weaver
                # is whoever ran the spawn — the operator, not the shard's agent.
                # Legacy left weaver to the server's X-Agent-Id (the current CLI
                # agent), keeping from_id = the spawned agent; preserve that split.
                thread_hash = station.store.save_thread(
                    from_id=spawn_agent,
                    to_id=brief if brief else spawn_agent,
                    type="tag",
                    weaver=_default_author(),
                    content=thread_content,
                )
            shard_info["thread_id"] = thread_hash
        except Exception as e:
            # Don't fail spawn if thread creation fails
            click.echo(f"Warning: Failed to create SKEIN thread: {e}", err=True)

        click.echo(f"✓ Spawned SHARD: {shard_info['shard_id']}")
        click.echo(f"  Name: {shard_info['name']}")
        click.echo(f"  Branch: {shard_info['branch_name']}")
        click.echo(f"  Worktree: {shard_info['worktree_path']}")
        if shard_info.get("brief_id"):
            click.echo(f"  Brief: {shard_info['brief_id']}")
        if shard_info.get("thread_id"):
            click.echo(f"  Thread: {shard_info['thread_id']}")
        click.echo("\nTo work in this SHARD:")
        click.echo(f"  cd {shard_info['worktree_path']}")

    except shard_engine.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to spawn SHARD: {e}")


@shard.command("list")
@click.option("--active", is_flag=True, help="Show only active SHARDs")
@click.option("--agent", "filter_agent", help="Filter by shard name")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def shard_list(ctx, active, filter_agent, output_json):
    """
    List SHARD worktrees.

    Example:
        skein shard list
        skein shard list --agent opus-security-architect
    """
    try:
        shards = shard_engine.list_shards(active_only=active)

        if filter_agent:
            shards = [s for s in shards if s["name"] == filter_agent]

        if output_json:
            click.echo(json.dumps(shards, indent=2))
        else:
            if not shards:
                click.echo("No SHARDs found")
            else:
                for shard_item in shards:
                    click.echo(shard_item["worktree_name"])

            click.echo()
            click.echo(
                "Tip: Use `skein shard triage` for actionable overview with status and tender info"
            )
            return

    except shard_engine.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to list SHARDs: {e}")


@shard.command("show")
@click.argument("worktree_name")
@click.pass_context
def shard_show(ctx, worktree_name):
    """
    Show details of a specific SHARD.

    Example:
        skein shard show opus-security-architect-20251109-001
    """
    try:
        shard_status = shard_engine.get_shard_status(worktree_name)
        if not shard_status:
            raise click.ClickException(f"SHARD not found: {worktree_name}")

        git_info = shard_engine.get_shard_git_info(worktree_name)

        click.echo(f"{shard_status['worktree_name']} ({shard_status['branch_name']})")
        click.echo()

        uncommitted = git_info.get("uncommitted", [])
        if uncommitted:
            click.echo("Uncommitted:")
            for line in uncommitted:
                click.echo(f" {line}")
            click.echo()

        commit_log = git_info.get("commit_log", [])
        if commit_log:
            for sha, msg in commit_log:
                click.echo(f"{sha} {msg}")
            click.echo()
            diffstat = git_info.get("diffstat", "")
            if diffstat:
                click.echo(diffstat)
                click.echo()
        else:
            tip_sha = git_info.get("tip_sha", "")
            tip_msg = git_info.get("tip_message", "")
            tip_in_master = git_info.get("tip_in_master", False)
            commits_behind = git_info.get("commits_behind", 0)
            if tip_sha:
                click.echo(f"Tip: {tip_sha} {tip_msg}")
                if tip_in_master:
                    if commits_behind > 0:
                        click.echo(f"     (in master, {commits_behind} commits behind HEAD)")
                    else:
                        click.echo("     (in master)")
                click.echo()
            click.echo("No unique commits.")
            click.echo()

        status_parts = []
        working = git_info.get("working_tree", "unknown")
        if working == "clean" and not uncommitted:
            status_parts.append("Working tree clean")
        merge = git_info.get("merge_status", "unknown")
        if merge == "conflict":
            status_parts.append("Has conflicts")
        elif merge == "clean" and commit_log:
            status_parts.append("Merges clean")
        if status_parts:
            click.echo(", ".join(status_parts))

        if merge == "conflict":
            click.echo(f"\nTo see changes: skein shard diff {worktree_name}")

    except shard_engine.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to show SHARD: {e}")


@shard.command("diff")
@click.argument("worktree_name")
@click.option("--stat", "show_stat", is_flag=True, help="Show diffstat only")
@click.option(
    "--integration",
    is_flag=True,
    help="Show full integration diff with base branch (includes base branch evolution)",
)
@click.pass_context
def shard_diff(ctx, worktree_name, show_stat, integration):
    """
    Show diff for a SHARD.

    By default shows WORK DIFF: agent's actual changes from the base commit.
    This excludes any changes from base branch evolution (no false deletions).

    Use --integration to see what would actually merge into the current base branch.

    Examples:
        skein shard diff my-shard-001          # Work diff (agent's changes)
        skein shard diff my-shard-001 --stat   # Work diff stats only
        skein shard diff my-shard-001 --integration  # Full merge preview
    """
    try:
        shard_status = shard_engine.get_shard_status(worktree_name)
        if not shard_status:
            raise click.ClickException(f"SHARD not found: {worktree_name}")

        base_branch = shard_engine._get_shard_base_branch(worktree_name)

        if integration:
            click.echo(f"=== INTEGRATION DIFF: {worktree_name} ===\n")
            click.echo(f"Changes relative to current {base_branch}:\n")
            diff_output = shard_engine.get_shard_diff(
                worktree_name, stat_only=show_stat, integration=True
            )
            if diff_output:
                click.echo(diff_output)
            else:
                click.echo(f"No changes from current {base_branch}.")
        else:
            drift_info = shard_engine.get_shard_drift_info(worktree_name)
            if drift_info.get("has_metadata") and drift_info.get("base_commit"):
                click.echo(f"=== WORK DIFF: {worktree_name} ===\n")
                base_short = drift_info.get("base_commit_short", "unknown")
                base_date = drift_info.get("base_commit_date", "")
                click.echo(f"Changes from base commit {base_short}")
                if base_date:
                    click.echo(f"(Base created: {base_date})\n")
                master_ahead = drift_info.get("base_commits_ahead", 0)
                if master_ahead > 0:
                    click.echo(
                        f"Note: {base_branch} has {master_ahead} new commits since your base."
                    )
                    notable = drift_info.get("base_notable_changes", [])
                    if notable:
                        click.echo(f"Notable changes on {base_branch}:")
                        for change in notable[:5]:
                            click.echo(f"  - {change}")
                    click.echo()
                diff_output = shard_engine.get_shard_work_diff(worktree_name, stat_only=show_stat)
            else:
                click.echo(f"=== DIFF: {worktree_name} ===\n")
                click.echo(f"(No base commit metadata - showing diff from current {base_branch})\n")
                diff_output = shard_engine.get_shard_diff(worktree_name, stat_only=show_stat)

            if diff_output:
                click.echo(diff_output)
            else:
                click.echo("No changes.")

    except shard_engine.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to get diff: {e}")


@shard.command("cleanup")
@click.argument("worktree_name")
@click.option("--keep-branch", is_flag=True, help="Keep git branch after removing worktree")
@click.option("--chain", is_flag=True, help="Remove entire graft chain (original + all grafts)")
@click.option(
    "--caller-cwd",
    "explicit_caller_cwd",
    default=None,
    help="Original working directory of caller (for orchestration tools)",
)
@click.option(
    "--yes",
    "-y",
    "assume_yes",
    is_flag=True,
    help="Skip confirmation prompt",
)
@click.pass_context
def shard_cleanup(ctx, worktree_name, keep_branch, chain, explicit_caller_cwd, assume_yes):
    """
    Remove SHARD worktree and optionally delete branch.

    Use --chain to remove an entire graft chain (original + all grafts).

    Example:
        skein shard cleanup my-shard-001
        skein shard cleanup my-shard-001 --keep-branch
        skein shard cleanup my-shard-001 --chain   # Remove full graft lineage

    For orchestration tools (e.g., Spindle), pass --caller-cwd to prevent
    agents from deleting their own worktree after cd-ing elsewhere.
    """
    caller_cwd = explicit_caller_cwd if explicit_caller_cwd else os.getcwd()

    try:
        if not assume_yes:
            click.confirm("Are you sure you want to cleanup this SHARD?", abort=True)

        if chain:
            result = shard_engine.cleanup_graft_chain(
                worktree_name, keep_branch=keep_branch, caller_cwd=caller_cwd
            )
            click.echo(f"Tracing worktree chain for: {worktree_name}\n")
            if result["removed"]:
                click.echo(f"Found chain ({len(result['removed'])} worktrees):")
                for wt in reversed(result["removed"]):
                    label = "(original)" if not shard_engine.is_graft(wt) else "(graft)"
                    click.echo(f"  {wt} {label}")
                click.echo()
                click.echo(f"Removed {len(result['removed'])} worktrees:")
                for wt in result["removed"]:
                    click.echo(f"  ✓ {wt}")
            if result["errors"]:
                click.echo("\nWarnings:")
                for err in result["errors"]:
                    click.echo(f"  ⚠ {err}", err=True)
            if not keep_branch:
                click.echo("\n  (Branches also deleted)")
        else:
            shard_engine.cleanup_shard(
                worktree_name, keep_branch=keep_branch, caller_cwd=caller_cwd
            )
            click.echo(f"✓ Cleaned up SHARD: {worktree_name}")
            if not keep_branch:
                click.echo("  (Branch also deleted)")
            else:
                click.echo("  (Branch kept)")
            if shard_engine.is_graft(worktree_name):
                root = shard_engine.get_graft_chain_root(worktree_name)
                remaining_chain = shard_engine.get_graft_chain(root)
                if remaining_chain:
                    click.echo("\nNote: Original shard and/or other grafts may remain:")
                    for wt in remaining_chain:
                        click.echo(f"  - {wt}")
                    click.echo(f"\nTo remove all: skein shard cleanup {root} --chain")

    except shard_engine.ShardError as e:
        raise click.ClickException(str(e))
    except click.Abort:
        raise
    except Exception as e:
        raise click.ClickException(f"Failed to cleanup SHARD: {e}")


@shard.command("graft")
@click.argument("worktree_name")
@click.pass_context
def shard_graft(ctx, worktree_name):
    """
    Create a graft worktree to resolve conflicts with the base branch.

    When a shard has conflicts with its base branch (due to branch evolution),
    grafting creates a new worktree from the current base branch and cherry-picks
    the shard's commits onto it.

    If conflicts occur during cherry-pick, the graft is left in a conflicted
    state for you to resolve manually.

    Example:
        skein shard graft my-shard-001

    After resolving conflicts (if any):
        cd worktrees/my-shard-001-graft/
        git add <resolved files>
        git commit
        skein shard merge my-shard-001-graft

    Grafts can be grafted - if the base branch evolves again before you merge,
    just run graft again on the graft:
        skein shard graft my-shard-001-graft
    """
    try:
        base_branch = shard_engine._get_shard_base_branch(worktree_name)
        click.echo(f"Creating graft worktree from current {base_branch}...")
        click.echo(f"Applying commits from {worktree_name}...")
        click.echo()

        result = shard_engine.graft_shard(worktree_name)

        if result["success"]:
            click.echo("✓ Applied cleanly (no conflicts)\n")
            click.echo("Graft created at:")
            click.echo(f"  {result['graft_worktree_path']}/\n")
            click.echo(f"Your work has been applied onto current {base_branch}.")
            click.echo("Review and test, then merge:")
            click.echo(f"  cd {result['graft_worktree_path']}")
            click.echo("  (run tests)")
            click.echo(f"  skein shard merge {result['graft_worktree_name']}")
        elif not result.get("conflicts"):
            applied = result.get("commits_applied", 0)
            total = result.get("commits_total", applied)
            pending = result.get("commits_pending", 0)
            click.echo("⏸ Graft PAUSED mid-sequence (no merge conflicts)\n")
            click.echo("Graft created at:")
            click.echo(f"  {result['graft_worktree_path']}/\n")
            click.echo(
                f"{applied}/{total} commit(s) applied, "
                f"{pending} still queued in the cherry-pick sequencer.\n"
            )
            click.echo(
                "A commit replayed empty on the new base (its change is already\n"
                "present), so git stopped without creating it.\n"
            )
            click.echo("Continue the sequence:")
            click.echo(f"  cd {result['graft_worktree_path']}")
            click.echo("  git cherry-pick --skip       # drop the now-empty commit")
            click.echo("  (or `git cherry-pick --continue` to keep it as an empty commit)")
            click.echo("  (repeat until the sequencer is empty - do NOT stop early)\n")
            click.echo("Then merge:")
            click.echo(f"  skein shard merge {result['graft_worktree_name']}")
        else:
            click.echo(f"✗ Conflicts in: {', '.join(result['conflicts'])}\n")
            click.echo("Graft created at:")
            click.echo(f"  {result['graft_worktree_path']}/\n")
            depth = result.get("chain_depth", 0)
            if depth > 1:
                chain = shard_engine.get_graft_chain(
                    shard_engine.get_graft_chain_root(worktree_name)
                )
                click.echo("Chain: " + " → ".join(chain))
                click.echo()
            applied = result.get("commits_applied", 0)
            total = result.get("commits_total", applied)
            pending = result.get("commits_pending", 0)
            click.echo(
                f"Graft PAUSED mid-sequence: {applied}/{total} commit(s) applied, "
                f"{pending} still queued in the cherry-pick sequencer.\n"
            )
            click.echo("Resolve conflicts:")
            click.echo(f"  cd {result['graft_worktree_path']}")
            for f in result["conflicts"]:
                click.echo(f"  (edit {f} to resolve conflicts)")
            click.echo("  git add <resolved files>")
            click.echo("  git cherry-pick --continue")
            click.echo(
                "  (repeat resolve + --continue for any further conflicts until the\n"
                "   sequencer is empty - do NOT stop after the first commit)\n"
            )
            click.echo("Then merge:")
            click.echo(f"  skein shard merge {result['graft_worktree_name']}")

    except shard_engine.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to create graft: {e}")


@shard.command("merge")
@click.argument("worktree_name")
@click.option(
    "--caller-cwd",
    "explicit_caller_cwd",
    default=None,
    help="Original working directory of caller (for orchestration tools)",
)
@click.pass_context
def shard_merge(ctx, worktree_name, explicit_caller_cwd):
    """
    Merge SHARD branch into its base branch and cleanup.

    Refuses if there are uncommitted changes or conflicts.
    After successful merge, posts a tender folio (status=complete) and auto-closes it.

    If conflicts are detected, suggests using 'skein shard graft' instead.

    Example:
        skein shard merge beadle_0001-20251202-001

    For orchestration tools (e.g., Spindle), pass --caller-cwd to prevent
    agents from merging their own worktree after cd-ing elsewhere.
    """
    caller_cwd = explicit_caller_cwd if explicit_caller_cwd else os.getcwd()

    # Gather tender metadata BEFORE merge (worktree gets deleted during merge)
    shard_info = shard_engine.get_shard_status(worktree_name)
    if not shard_info:
        raise click.ClickException(f"SHARD not found: {worktree_name}")

    try:
        metadata = shard_engine.get_tender_metadata(worktree_name)
    except Exception:
        metadata = None

    try:
        drift_info = shard_engine.get_shard_drift_info(worktree_name)
        master_ahead = drift_info.get("base_commits_ahead", 0)
        base_branch = drift_info.get("base_branch")
        if base_branch is None:
            try:
                base_branch = shard_engine._detect_default_branch()
            except Exception:
                base_branch = "unknown"

        click.echo(f"Testing integration with current {base_branch}...")

        result = shard_engine.merge_shard(worktree_name, caller_cwd=caller_cwd)

        if result["success"]:
            if master_ahead > 0:
                click.echo(
                    f"✓ Clean integration (applied onto current {base_branch}, {master_ahead} commits ahead)"
                )
            else:
                click.echo("✓ Clean integration")
            click.echo()
            click.echo(f"Merging to {base_branch}...")
            click.echo(result["message"])

            tender_id = _post_merge_tender(ctx, worktree_name, shard_info, metadata)
            if tender_id:
                click.echo(f"  Tender: {tender_id} (auto-closed)")

            if shard_engine.is_graft(worktree_name):
                root = shard_engine.get_graft_chain_root(worktree_name)
                chain = shard_engine.get_graft_chain(root)
                if chain:
                    click.echo("\nCleanup worktree chain:")
                    click.echo(f"  → skein shard cleanup {root} --chain")
                    click.echo("\nThis will remove:")
                    for wt in chain:
                        click.echo(f"  - worktrees/{wt}/")
            else:
                click.echo("\nCleanup worktree:")
                click.echo(f"  → skein shard cleanup {worktree_name}")
        else:
            click.echo(f"✗ {result['message']}")
            if result.get("uncommitted"):
                click.echo("\nUncommitted files:")
                for f in result["uncommitted"]:
                    click.echo(f"  {f}")
                click.echo("\nCommit your changes first, then retry merge.")
            if result.get("conflicts"):
                click.echo("\n✗ Conflicts detected")
                for f in result["conflicts"]:
                    click.echo(f"  - {f}")
                click.echo("\nCreate graft worktree to resolve:")
                click.echo(f"  → skein shard graft {worktree_name}")
            raise SystemExit(1)

    except shard_engine.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to merge SHARD: {e}")


def _derive_tender_site(worktree_path: str) -> str:
    """Derive a default tender site slug from a worktree path (legacy rule)."""
    if "/projects/" in worktree_path:
        parts = worktree_path.split("/projects/")[1].split("/")
        if parts:
            return f"{parts[0]}-development"
    return "shard-review"


def _post_merge_tender(ctx, worktree_name, shard_info, metadata):
    """
    Post a tender folio (status=complete) after a successful merge, then close it.

    Returns the tender folio hash on success, None on failure (non-fatal — the
    merge already succeeded). A missing target site is one such non-fatal case.
    """
    site = _derive_tender_site(shard_info.get("worktree_path", ""))

    if metadata:
        summary_text = metadata.get("last_commit_message", "Merged")
        files_list = metadata.get("files_modified", [])
        commits = metadata.get("commits", 0)
        branch_name = metadata.get("branch_name", shard_info.get("branch_name", "unknown"))
    else:
        summary_text = "Merged"
        files_list = []
        commits = 0
        branch_name = shard_info.get("branch_name", "unknown")

    files_str = "\n".join(f"  - {f}" for f in files_list[:20])
    if len(files_list) > 20:
        files_str += f"\n  ... and {len(files_list) - 20} more"

    body = f"""## Tender: {worktree_name}

**Status:** complete (merged)

### Summary
{summary_text}

### Changes
- **Commits:** {commits}
- **Branch:** {branch_name}

### Files Modified
{files_str if files_str else "  (none)"}
"""

    meta = {
        "worktree_name": worktree_name,
        "branch_name": branch_name,
        "commits": commits,
        "files_modified": files_list,
        "status": "complete",
        "merged": True,
        "name": shard_info.get("name"),
    }
    content = render_tender_content(body, meta)
    title = make_title_from_content(summary_text) if summary_text else f"Merged: {worktree_name}"
    author = _default_author()

    try:
        with _open_station(ctx) as station:
            tender_hash = station.post(
                type="tender",
                site=site,
                title=title,
                content=content,
                created_by=author,
            )
            station.set_status(tender_hash, "closed", by=author)
        return tender_hash
    except Exception:
        # Tender posting is non-fatal - merge already succeeded.
        return None


@shard.command("pause")
@click.argument("worktree_name")
@click.argument("reason")
@click.pass_context
def shard_pause(ctx, worktree_name, reason):
    """
    Pause work on a SHARD.

    Example:
        skein shard pause opus-security-architect-20251109-001 "Blocked on bubblewrap version decision"
    """
    shard_info = shard_engine.get_shard_status(worktree_name)
    if not shard_info:
        raise click.ClickException(f"SHARD not found: {worktree_name}")

    try:
        with _open_station(ctx) as station:
            shard_thread = _find_shard_thread(station, worktree_name)
            if shard_thread:
                station.store.save_thread(
                    from_id=_default_author(),
                    to_id=shard_thread,
                    type="reply",
                    weaver=_default_author(),
                    content=f"[PAUSED] {reason}",
                )
        click.echo(f"⏸  Paused SHARD: {worktree_name}")
        click.echo(f"  Reason: {reason}")
    except Exception as e:
        click.echo(f"⏸  Paused SHARD: {worktree_name}")
        click.echo(f"  Reason: {reason}")
        click.echo(f"  Warning: Failed to update SKEIN thread: {e}", err=True)


@shard.command("resume")
@click.argument("worktree_name")
@click.argument("message", required=False)
@click.pass_context
def shard_resume(ctx, worktree_name, message):
    """
    Resume work on a paused SHARD.

    Examples:
        skein shard resume opus-security-architect-20251109-001 "Decision made: use bubblewrap 0.5.0"
        skein shard resume opus-security-architect-20251109-001
    """
    shard_info = shard_engine.get_shard_status(worktree_name)
    if not shard_info:
        raise click.ClickException(f"SHARD not found: {worktree_name}")

    try:
        with _open_station(ctx) as station:
            shard_thread = _find_shard_thread(station, worktree_name)
            if shard_thread:
                resume_msg = f"[RESUMED] {message}" if message else "[RESUMED]"
                station.store.save_thread(
                    from_id=_default_author(),
                    to_id=shard_thread,
                    type="reply",
                    weaver=_default_author(),
                    content=resume_msg,
                )
        click.echo(f"▶  Resumed SHARD: {worktree_name}")
        if message:
            click.echo(f"  Message: {message}")
    except Exception as e:
        click.echo(f"▶  Resumed SHARD: {worktree_name}")
        if message:
            click.echo(f"  Message: {message}")
        click.echo(f"  Warning: Failed to update SKEIN thread: {e}", err=True)


def _find_shard_thread(station: Station, worktree_name: str) -> Optional[str]:
    """The hash of the ``tag`` thread tracking ``worktree_name``, or None.

    Mirrors legacy pause/resume: scan ``tag`` threads, parse each one's JSON
    content, and match on the ``shard`` tag plus ``worktree_name``.
    """
    for thread in station.store.get_threads(type="tag"):
        try:
            content = json.loads(thread.get("content") or "{}")
        except json.JSONDecodeError:
            continue
        if content.get("tag") == "shard" and content.get("worktree_name") == worktree_name:
            return thread.get("thread_hash")
    return None


@shard.command("review")
@click.option(
    "--stale-days",
    default=7,
    type=int,
    help="Days without commits to consider stale (default: 7)",
)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def shard_review(ctx, stale_days, output_json):
    """
    Show SHARD review queue for QM visibility.

    Groups shards by status:
    - READY: Has commits, clean working tree, no conflicts (merge candidates)
    - NEEDS_COMMIT: Has uncommitted changes
    - CONFLICTS: Would have merge conflicts with master
    - STALE: No commits and older than --stale-days

    Examples:
        skein shard review
        skein shard review --stale-days 3
        skein shard review --json
    """
    try:
        queue = shard_engine.get_review_queue(stale_days=stale_days)

        if output_json:
            click.echo(json.dumps(queue, indent=2))
            return

        total = sum(len(v) for v in queue.values())
        if total == 0:
            click.echo("No SHARDs found")
            return

        click.echo("=" * 60)
        click.echo("SHARD Review Queue")
        click.echo("=" * 60)
        click.echo(f"Total: {total} shards")
        click.echo(
            f"  Ready: {len(queue['ready'])}  |  Needs commit: {len(queue['needs_commit'])}  |  Conflicts: {len(queue['conflicts'])}  |  Stale: {len(queue['stale'])}"
        )
        click.echo()

        def format_shard_line(shard_item):
            name = shard_item["worktree_name"]
            commits = shard_item.get("commits_ahead", 0)
            age = shard_item.get("age_days")
            age_str = f"{age}d" if age is not None else "?"
            path = shard_item.get("worktree_path", "")
            project = "?"
            if "/projects/" in path:
                parts = path.split("/projects/")[1].split("/")
                if parts:
                    project = parts[0]
            diffstat = shard_item.get("diffstat", "")
            files_changed = 0
            insertions = 0
            deletions = 0
            if diffstat:
                lines = diffstat.strip().split("\n")
                if lines:
                    last_line = lines[-1]
                    if "changed" in last_line:
                        m = re.search(r"(\d+) files? changed", last_line)
                        if m:
                            files_changed = int(m.group(1))
                        m = re.search(r"(\d+) insertions?", last_line)
                        if m:
                            insertions = int(m.group(1))
                        m = re.search(r"(\d+) deletions?", last_line)
                        if m:
                            deletions = int(m.group(1))
            diff_str = ""
            if files_changed > 0:
                diff_str = f"{files_changed}f +{insertions}/-{deletions}"
            return f"  {name:<40} {age_str:>4}  +{commits:<2}  {project:<15} {diff_str}"

        categories = [
            ("READY", "ready", "Merge candidates - clean and ready"),
            ("NEEDS_COMMIT", "needs_commit", "Have uncommitted changes"),
            ("CONFLICTS", "conflicts", "Would conflict with base branch"),
            ("STALE", "stale", f"No commits, older than {stale_days} days"),
        ]
        for label, key, description in categories:
            shards = queue[key]
            if not shards:
                continue
            click.echo(f"--- {label} ({len(shards)}) - {description} ---")
            for shard_item in shards:
                click.echo(format_shard_line(shard_item))
            click.echo()

        click.echo("Commands:")
        click.echo("  skein shard show <name>    # View details")
        click.echo("  skein shard diff <name>    # View changes")
        click.echo("  skein shard merge <name>   # Merge to base branch")

    except shard_engine.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to get review queue: {e}")


@shard.command("tender")
@click.argument("worktree_name")
@click.option("--site", help="Site to post tender folio (default: derived from project)")
@click.option("--reviewer", help="Agent ID to review this SHARD (default: prime)")
@click.option("--summary", help="Brief summary of changes")
@click.option(
    "--status",
    type=click.Choice(["complete", "incomplete", "abandoned"]),
    default="complete",
    help="Work status: complete (default), incomplete, or abandoned",
)
@click.option(
    "--confidence",
    type=click.IntRange(1, 10),
    help="Merge confidence 1-10: 10=safe/additive/isolated (auto-merge candidate), "
    "5=moderate risk (needs review), 1=hot mess/critical path (careful review needed)",
)
@click.pass_context
def shard_tender(ctx, worktree_name, site, reviewer, summary, status, confidence):
    """
    Mark SHARD as ready for review (tender for assessment).

    Creates a tender folio visible to QMs and reviewers.

    Examples:
        skein shard tender my-shard-001
        skein shard tender my-shard --summary "Added auth checks" --confidence 8
        skein shard tender my-shard --site speakbot-pm --status incomplete
    """
    shard_info = shard_engine.get_shard_status(worktree_name)
    if not shard_info:
        raise click.ClickException(f"SHARD not found: {worktree_name}")

    try:
        metadata = shard_engine.get_tender_metadata(worktree_name)
        if not metadata:
            raise click.ClickException(f"Could not gather metadata for {worktree_name}")
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(f"Failed to gather metadata: {e}")

    with _open_station(ctx) as station:
        # Only true sites are valid tender targets. The slug table is not
        # site-exclusive (Stage 2 registers agent names as slugs too), so filter
        # to type=site folios — the faithful equivalent of legacy GET /sites,
        # which never surfaced non-site slugs.
        available_site_ids = [
            slug for slug, folio in station.list_sites() if folio and folio.get("type") == "site"
        ]

        if not site:
            derived = None
            worktree_path = shard_info.get("worktree_path", "")
            if "/projects/" in worktree_path:
                parts = worktree_path.split("/projects/")[1].split("/")
                if parts:
                    derived = f"{parts[0]}-development"
            if derived and derived in available_site_ids:
                site = derived
            elif "shard-review" in available_site_ids:
                site = "shard-review"
            else:
                tried = [derived] if derived else []
                tried.append("shard-review")
                raise click.ClickException(
                    f"No default site found — tried {', '.join(repr(t) for t in tried)}.\n"
                    f"Available sites: {', '.join(available_site_ids) or '(none)'}\n"
                    f"Pass one explicitly via: skein shard tender {worktree_name} --site <site>"
                )
        elif site not in available_site_ids:
            raise click.ClickException(
                f"Site '{site}' not found in this project.\n"
                f"Available sites: {', '.join(available_site_ids) or '(none)'}\n"
                f"Pass a valid site name from the list above: skein shard tender {worktree_name} --site <site>"
            )

        if not reviewer:
            reviewer = "prime"

        summary_text = summary or metadata.get("last_commit_message", "No summary provided")
        files_list = metadata.get("files_modified", [])
        files_str = "\n".join(f"  - {f}" for f in files_list[:20])
        if len(files_list) > 20:
            files_str += f"\n  ... and {len(files_list) - 20} more"

        body = f"""## Tender: {worktree_name}

**Status:** {status}
**Confidence:** {confidence or "unrated"}/10
**Reviewer:** {reviewer}

### Summary
{summary_text}

### Changes
- **Commits:** {metadata.get("commits", 0)}
- **Branch:** {metadata.get("branch_name", "unknown")}

### Files Modified
{files_str if files_str else "  (none)"}
"""

        meta = {
            "worktree_name": worktree_name,
            "branch_name": metadata.get("branch_name"),
            "commits": metadata.get("commits", 0),
            "files_modified": files_list,
            "status": status,
            "confidence": confidence,
            "reviewer": reviewer,
            "name": metadata.get("name"),
        }
        content = render_tender_content(body, meta)
        title = (
            make_title_from_content(summary_text)
            if summary_text
            else f"Shard tender: {worktree_name}"
        )

        try:
            folio_id = station.post(
                type="tender",
                site=site,
                title=title,
                content=content,
                created_by=_default_author(),
            )
        except UnknownSite as e:
            raise click.ClickException(str(e))
        except Exception as e:
            raise click.ClickException(f"Failed to create tender folio: {e}")

    click.echo(f"Tendered SHARD: {worktree_name}")
    click.echo(f"  Folio: {folio_id}")
    click.echo(f"  Site: {site}")
    click.echo(f"  Status: {status}")
    if confidence is not None:
        click.echo(f"  Confidence: {confidence}/10")
    click.echo(f"  Reviewer: {reviewer}")
    click.echo(f"  Commits: {metadata.get('commits', 0)}")
    click.echo(f"  Files: {len(files_list)}")
    if summary:
        click.echo(f"  Summary: {summary}")
    click.echo(f"\n  View: skein folio {folio_id}")
    click.echo(f"  List tenders: skein folios {site} --type tender")


@shard.command("triage")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def shard_triage(ctx, output_json):
    """
    Triage all SHARDs - actionable overview with status, conflicts, drift, and tender info.

    Shows all shards with:
    - Commit count and diffstat (+/-)
    - Merge status (clean/CONFLICT/uncommitted)
    - Drift info (master commits since base)
    - Graft chain context (if applicable)
    - Tender confidence if exists

    Example:
        skein shard triage
        skein shard triage --json
    """
    try:
        shards = shard_engine.list_shards(active_only=True)
        if not shards:
            click.echo("No SHARDs found")
            return

        # Newest tender per worktree (append-only tenders; newest wins).
        tender_map: Dict[str, Dict[str, Any]] = {}
        try:
            with _open_station(ctx) as station:
                latest = latest_tenders_by_worktree(station.folios_by_type("tender"))
            for wt_name, folio in latest.items():
                meta = folio["meta"]
                tender_map[wt_name] = {
                    "folio_id": folio.get("content_hash"),
                    "confidence": meta.get("confidence"),
                    "status": meta.get("status"),
                    "summary": (folio.get("title") or "")[:50],
                }
        except Exception:
            pass  # Tender lookup is optional

        triage_data = []
        for shard_item in shards:
            wt_name = shard_item["worktree_name"]
            git_info = shard_engine.get_shard_git_info(wt_name)
            drift_info = shard_engine.get_shard_drift_info(wt_name)

            commits = git_info.get("commits_ahead", 0)
            uncommitted = git_info.get("uncommitted", [])

            master_ahead = drift_info.get("base_commits_ahead", 0)
            base_commit = drift_info.get("base_commit_short")
            conflict_status = drift_info.get("conflict_status", "unknown")
            conflict_files = drift_info.get("conflict_files", [])
            base_branch = drift_info.get("base_branch")
            if base_branch is None:
                try:
                    base_branch = shard_engine._detect_default_branch()
                except Exception:
                    base_branch = "unknown"

            is_graft = shard_engine.is_graft(wt_name)
            graft_depth = shard_engine.get_graft_depth(wt_name) if is_graft else 0

            diffstat = git_info.get("diffstat", "")
            insertions = 0
            deletions = 0
            if diffstat:
                ins_match = re.search(r"(\d+) insertions?\(\+\)", diffstat)
                del_match = re.search(r"(\d+) deletions?\(-\)", diffstat)
                if ins_match:
                    insertions = int(ins_match.group(1))
                if del_match:
                    deletions = int(del_match.group(1))

            if uncommitted:
                status_icon = "○"
                status_text = "uncommitted"
            elif conflict_status == "conflict":
                status_icon = "⚠"
                status_text = "CONFLICT"
            elif commits == 0:
                status_icon = "·"
                status_text = "empty"
            else:
                status_icon = "✓"
                status_text = "clean"

            tender = tender_map.get(wt_name)
            confidence = tender.get("confidence") if tender else None

            entry = {
                "worktree_name": wt_name,
                "commits": commits,
                "insertions": insertions,
                "deletions": deletions,
                "status": status_text,
                "status_icon": status_icon,
                "confidence": confidence,
                "tender_id": tender.get("folio_id") if tender else None,
                "base_ahead": master_ahead,
                "base_branch": base_branch,
                "base_commit": base_commit,
                "is_graft": is_graft,
                "graft_depth": graft_depth,
                "conflict_status": conflict_status,
                "conflict_files": conflict_files,
            }
            triage_data.append(entry)

        if output_json:
            click.echo(json.dumps(triage_data, indent=2))
        else:
            click.echo(f"SHARDS ({len(triage_data)} total):\n")
            for entry in triage_data:
                name = entry["worktree_name"]
                commits = entry["commits"]
                ins = entry["insertions"]
                dels = entry["deletions"]
                status = entry["status"]
                icon = entry["status_icon"]
                conf = entry["confidence"]
                base_ahead = entry.get("base_ahead", 0)
                base_branch = entry.get("base_branch")
                if base_branch is None:
                    try:
                        base_branch = shard_engine._detect_default_branch()
                    except Exception:
                        base_branch = "unknown"
                base_commit = entry.get("base_commit")
                is_graft = entry.get("is_graft", False)

                diffstat_str = f"+{ins}/-{dels}" if (ins or dels) else "---"
                if is_graft:
                    icon = "○"

                parts = [
                    f"  {icon}",
                    f"{name:<40}",
                    f"{status:<12}",
                    f"{commits:>2} commits",
                    f"{diffstat_str:>12}",
                ]
                click.echo("  ".join(parts))

                context_parts = []
                if base_commit:
                    context_parts.append(f"base: {base_commit}")
                if base_ahead > 0:
                    conflict_status_val = entry.get("conflict_status", "unknown")
                    if conflict_status_val == "conflict":
                        context_parts.append(f"{base_branch} +{base_ahead} (conflicts)")
                    else:
                        context_parts.append(f"{base_branch} +{base_ahead} (no conflicts)")
                if is_graft:
                    root = shard_engine.get_graft_chain_root(name)
                    context_parts.append(f"graft of {root}")
                if context_parts:
                    click.echo(f"       {', '.join(context_parts)}")

                conflict_status_val = entry.get("conflict_status", "unknown")
                conflict_files_list = entry.get("conflict_files", [])
                if status == "CONFLICT" and base_ahead == 0 and not is_graft:
                    click.echo(
                        f"       conflicts with {base_branch} (files: {', '.join(conflict_files_list[:3])}{'...' if len(conflict_files_list) > 3 else ''})"
                    )
                elif status == "CONFLICT" and conflict_files_list:
                    files_str = ", ".join(conflict_files_list[:3])
                    if len(conflict_files_list) > 3:
                        files_str += f" +{len(conflict_files_list) - 3} more"
                    click.echo(f"       conflicting files: {files_str}")

                if conf is not None:
                    click.echo(f"       confidence: {conf}/10")
                elif entry["tender_id"]:
                    click.echo(f"       tendered: {entry['tender_id']}")

                click.echo()

            click.echo("Commands:")
            click.echo("  skein shard review <name>    # View details")
            click.echo("  skein shard diff <name>      # View work diff")
            click.echo("  skein shard merge <name>     # Merge to base branch")
            click.echo("  skein shard graft <name>     # Create graft to resolve conflicts")

    except shard_engine.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to triage SHARDs: {e}")


@shard.command("inspect")
@click.argument("worktree_name")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def shard_inspect(ctx, worktree_name, output_json):
    """
    Deep review of a single SHARD for merge decision.

    Shows:
    - Work diff (agent's actual changes from base)
    - Master activity since base (drift info)
    - Conflict status with specific files
    - Graft chain context (if applicable)
    - Tender summary and confidence

    Example:
        skein shard inspect my-shard-001
    """
    try:
        shard_info = shard_engine.get_shard_status(worktree_name)
        if not shard_info:
            raise click.ClickException(f"SHARD not found: {worktree_name}")

        git_info = shard_engine.get_shard_git_info(worktree_name)
        drift_info = shard_engine.get_shard_drift_info(worktree_name)

        # Newest tender for THIS worktree (newest wins, not server order).
        tender_info = None
        try:
            with _open_station(ctx) as station:
                latest = latest_tenders_by_worktree(station.folios_by_type("tender"))
            folio = latest.get(worktree_name)
            if folio:
                meta = folio["meta"]
                tender_info = {
                    "folio_id": folio.get("content_hash"),
                    "confidence": meta.get("confidence"),
                    "status": meta.get("status"),
                    "summary": folio.get("title", ""),
                }
        except Exception:
            pass

        conflict_status = drift_info.get("conflict_status", "unknown")
        conflict_files = drift_info.get("conflict_files", [])

        is_nested = drift_info.get("is_nested", False)
        review_data = {
            "worktree_name": worktree_name,
            "branch_name": shard_info["branch_name"],
            "worktree_path": shard_info["worktree_path"],
            "commits_ahead": git_info.get("commits_ahead", 0),
            "merge_status": conflict_status,
            "uncommitted": git_info.get("uncommitted", []),
            "commit_log": git_info.get("commit_log", []),
            "diffstat": git_info.get("diffstat", ""),
            "conflict_files": conflict_files,
            "tender": tender_info,
            "base_commit": drift_info.get("base_commit_short"),
            "base_commit_date": drift_info.get("base_commit_date"),
            "base_commits_ahead": drift_info.get("base_commits_ahead", 0),
            "base_notable_changes": drift_info.get("base_notable_changes", []),
            "is_graft": shard_engine.is_graft(worktree_name),
            "is_nested": is_nested,
            "work_diff_stat": drift_info.get("work_diff_stat"),
        }

        # Run xgun scan if available (silent degradation if not)
        xgun_result = None
        import shutil

        if shutil.which("xgun"):
            worktree_path = shard_info["worktree_path"]
            try:
                result = subprocess.run(
                    ["xgun", "scan", worktree_path, "--output", "json"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    cwd=worktree_path,
                )
                if result.returncode in (0, 1):
                    xgun_result = json.loads(result.stdout)
            except (
                subprocess.TimeoutExpired,
                subprocess.SubprocessError,
                json.JSONDecodeError,
            ):
                pass

        if xgun_result:
            review_data["xgun"] = xgun_result

        if output_json:
            click.echo(json.dumps(review_data, indent=2))
        else:
            click.echo(f"=== SHARD: {worktree_name} ===")
            click.echo()

            is_graft = shard_engine.is_graft(worktree_name)
            if is_graft:
                root = shard_engine.get_graft_chain_root(worktree_name)
                chain = shard_engine.get_graft_chain(root)
                click.echo(f"Chain: {' → '.join(chain)}")
                click.echo()

            base_commit = drift_info.get("base_commit_short")
            base_branch = drift_info.get("base_branch")
            if base_branch is None:
                try:
                    base_branch = shard_engine._detect_default_branch()
                except Exception:
                    base_branch = "unknown"
            base_date = drift_info.get("base_commit_date", "")
            commits = git_info.get("commits_ahead", 0)
            uncommitted = git_info.get("uncommitted", [])

            if uncommitted:
                click.echo("Your Work (has uncommitted changes):")
            elif conflict_status == "conflict":
                click.echo(f"Your Work (conflicts with {base_branch}):")
            else:
                click.echo("Your Work (clean, ready to integrate):")

            if base_commit:
                click.echo(f"  Base: {base_commit}" + (f" ({base_date})" if base_date else ""))
            click.echo(f"  Commits: {commits}")

            work_stat = drift_info.get("work_diff_stat")
            files_changed = 0
            if work_stat:
                lines = work_stat.strip().split("\n")
                if lines:
                    summary = lines[-1]
                    click.echo(f"  Changes: {summary.strip()}")
                    files_changed = len(lines) - 1 if len(lines) > 1 else 0
            if files_changed > 0:
                click.echo(f"  Files changed: {files_changed}")
            click.echo()

            if uncommitted:
                click.echo("UNCOMMITTED CHANGES:")
                for line in uncommitted[:10]:
                    click.echo(f"  {line}")
                if len(uncommitted) > 10:
                    click.echo(f"  ... and {len(uncommitted) - 10} more")
                click.echo()

            master_ahead = drift_info.get("base_commits_ahead", 0)
            if master_ahead > 0:
                click.echo(f"{base_branch} Activity Since Your Base:")
                click.echo(f"  {master_ahead} new commits merged to {base_branch}")
                click.echo()
                notable = drift_info.get("base_notable_changes", [])
                if notable:
                    click.echo("  Notable changes:")
                    for change in notable[:5]:
                        click.echo(f"    - {change}")
                    click.echo()
                if conflict_status == "conflict":
                    click.echo("  ⚠ Integration test: Conflicts detected")
                    if conflict_files:
                        for f in conflict_files[:10]:
                            click.echo(f"    - {f}")
                        if len(conflict_files) > 10:
                            click.echo(f"    ... and {len(conflict_files) - 10} more")
                elif is_nested and not is_graft:
                    click.echo("  ✓ Integration test: No conflicts detected")
                    click.echo("  ⚠ Nested shard: contains commits from parent shard")
                    click.echo("    Must graft to isolate your changes before merging")
                else:
                    click.echo("  ✓ Integration test: No conflicts detected")
                    if commits > 0:
                        click.echo(f"  ✓ Ready to merge onto current {base_branch}")
                    else:
                        click.echo("  ℹ No code changes (research/verification only)")
                click.echo()
            elif base_commit:
                if is_nested and not is_graft:
                    click.echo("⚠ Nested shard: contains commits from parent shard")
                    click.echo("  Must graft to isolate your changes before merging")
                elif commits > 0:
                    click.echo(f"✓ {base_branch} is at same state as your base")
                    click.echo("✓ Ready to merge")
                else:
                    click.echo(f"✓ {base_branch} is at same state as your base")
                    click.echo("ℹ No code changes (research/verification only)")
                click.echo()

            if tender_info:
                conf_str = (
                    f"{tender_info['confidence']}/10"
                    if tender_info.get("confidence")
                    else "unrated"
                )
                click.echo(f"Tender: {tender_info['folio_id']} (confidence: {conf_str})")
                if tender_info.get("summary"):
                    click.echo(f"  {tender_info['summary']}")
                click.echo()

            if xgun_result:
                click.echo("=== Code Quality (xgun) ===")
                click.echo()
                summary = xgun_result.get("summary", {})
                flags_count = summary.get("flags", 0)
                signals_count = summary.get("signals", 0)
                smells_count = summary.get("smells", 0)
                passed = summary.get("passed", True)
                if passed:
                    click.echo(
                        f"✓ Quality: Passed ({signals_count} signals, {flags_count} flags, {smells_count} smells)"
                    )
                else:
                    click.echo(
                        f"✗ Quality: Issues detected ({signals_count} signals, {flags_count} flags, {smells_count} smells)"
                    )
                qgun_data = xgun_result.get("qgun", {})
                flags = qgun_data.get("flags", [])
                if flags:
                    click.echo()
                    click.echo(f"Flags ({len(flags)}):")
                    for flag in flags[:10]:
                        loc = (
                            f"{flag.get('file', '?')}:{flag['line']}"
                            if flag.get("line")
                            else flag.get("file", "?")
                        )
                        click.echo(f"  {loc} [{flag.get('check', '?')}] {flag.get('message', '')}")
                    if len(flags) > 10:
                        click.echo(f"  ... and {len(flags) - 10} more")
                signals = qgun_data.get("signals", [])
                if signals:
                    click.echo()
                    click.echo(f"Signals ({len(signals)}):")
                    for signal in signals[:5]:
                        click.echo(f"  [{signal.get('check', '?')}] {signal.get('message', '')}")
                    if len(signals) > 5:
                        click.echo(f"  ... and {len(signals) - 5} more")
                sgun_data = xgun_result.get("sgun", {})
                smells = sgun_data.get("smells", [])
                if smells:
                    click.echo()
                    click.echo(f"Smells ({len(smells)}):")
                    for smell in smells[:5]:
                        loc = (
                            f"{smell.get('file', '?')}:{smell['line']}"
                            if smell.get("line")
                            else smell.get("file", "?")
                        )
                        click.echo(f"  {loc} [{smell.get('kind', '?')}] {smell.get('reason', '')}")
                    if len(smells) > 5:
                        click.echo(f"  ... and {len(smells) - 5} more")
                click.echo()

            if uncommitted:
                click.echo("Commit your changes first, then merge:")
                click.echo(f"  cd {shard_info['worktree_path']}")
                click.echo("  git add . && git commit")
                click.echo(f"  skein shard merge {worktree_name}")
            elif conflict_status == "conflict":
                click.echo("Create graft worktree to resolve:")
                click.echo(f"  → skein shard graft {worktree_name}")
                click.echo()
                click.echo("Or review your original work first:")
                click.echo(f"  → skein shard diff {worktree_name}")
            elif is_nested and not is_graft:
                click.echo("Graft to isolate your changes from parent shard:")
                click.echo(f"  → skein shard graft {worktree_name}")
                click.echo()
                click.echo("This will cherry-pick only your commits onto the base branch.")
            elif commits == 0:
                click.echo("Nothing to merge (research/verification shard):")
                click.echo(f"  → skein shard cleanup {worktree_name}")
            else:
                click.echo("Merge to base branch:")
                click.echo(f"  → skein shard merge {worktree_name}")
                if is_graft:
                    root = shard_engine.get_graft_chain_root(worktree_name)
                    click.echo()
                    click.echo("After merge, cleanup chain:")
                    click.echo(f"  → skein shard cleanup {root} --chain")

    except shard_engine.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"Failed to review SHARD: {e}")


@shard.command("stash")
@click.argument("description")
@click.option("--agent", "stash_agent", help="Agent ID for the new SHARD")
@click.option(
    "--base",
    "base_branch",
    default=None,
    help="Branch to stash onto (default: auto-detected from repo)",
)
@click.pass_context
def shard_stash(ctx, description, stash_agent, base_branch):
    """
    Stash uncommitted changes into a new SHARD.

    Creates a new shard worktree, moves your uncommitted changes there,
    and leaves your current branch clean.

    Example:
        skein shard stash "WIP: auth refactor"
        skein shard stash "WIP: auth refactor" --base main
    """
    try:
        repo = shard_engine._get_repo()

        status = repo.git.status("--porcelain")
        if not status.strip():
            raise click.ClickException("No uncommitted changes to stash")

        if not stash_agent:
            stash_agent = f"stash-{datetime.now().strftime('%m%d')}"

        if base_branch is None:
            base_branch = shard_engine._detect_default_branch(repo)

        new_shard = shard_engine.spawn_shard(
            stash_agent,
            description=description,
            base_branch=base_branch,
        )
        worktree_path = new_shard["worktree_path"]
        worktree_name = new_shard["worktree_name"]

        repo.git.stash("push", "-m", f"shard-stash: {description}")

        try:
            result = subprocess.run(
                ["git", "stash", "apply", "--index"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise Exception(result.stderr)
            repo.git.stash("drop")

            click.echo(f"✓ Stashed changes to SHARD: {worktree_name}")
            click.echo(f"  Path: {worktree_path}")
            click.echo(f"  Description: {description}")
            click.echo()
            click.echo("Your current branch is now clean.")
            click.echo(f"To continue work: cd {worktree_path}")
        except Exception as e:
            try:
                repo.git.stash("pop")
            except Exception:
                pass
            try:
                shard_engine.cleanup_shard(worktree_name, keep_branch=False)
            except Exception:
                pass
            raise click.ClickException(f"Failed to apply stash to new shard: {e}")

    except shard_engine.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        if "ClickException" in str(type(e).__name__):
            raise
        raise click.ClickException(f"Failed to stash: {e}")


@shard.command("apply")
@click.argument("worktree_name")
@click.option("--no-confirm", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def shard_apply(ctx, worktree_name, no_confirm):
    """
    Apply SHARD changes as uncommitted changes to current branch.

    Takes the diff between the base branch and the shard branch and applies it
    as uncommitted changes. Useful for cherry-picking from stale shards.

    Example:
        skein shard apply my-shard-001
    """
    try:
        shard_info = shard_engine.get_shard_status(worktree_name)
        if not shard_info:
            raise click.ClickException(f"SHARD not found: {worktree_name}")

        repo = shard_engine._get_repo()
        branch = shard_info["branch_name"]

        status = repo.git.status("--porcelain")
        if status.strip() and not no_confirm:
            click.echo("Warning: You have uncommitted changes.")
            if not click.confirm("Continue?"):
                raise click.ClickException("Aborted")

        base_branch = shard_engine._get_shard_base_branch(worktree_name)
        diff = repo.git.diff(base_branch, branch)
        if not diff.strip():
            click.echo(f"No changes in shard {worktree_name}")
            return

        git_info = shard_engine.get_shard_git_info(worktree_name)
        commits = git_info.get("commits_ahead", 0)
        click.echo(f"Applying changes from: {worktree_name} ({commits} commits)")

        if not no_confirm:
            if not click.confirm("Apply as uncommitted changes?"):
                raise click.ClickException("Aborted")

        try:
            if not diff.endswith("\n"):
                diff += "\n"
            result = subprocess.run(
                ["git", "apply"],
                input=diff,
                text=True,
                capture_output=True,
                cwd=shard_engine.get_project_root(),
            )
            if result.returncode != 0:
                raise Exception(result.stderr or "git apply failed")
            click.echo(f"✓ Applied changes from {worktree_name}")
            click.echo("  Review with `git status` and `git diff`")
        except Exception as e:
            raise click.ClickException(f"Failed to apply: {e}")

    except shard_engine.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        if "ClickException" in str(type(e).__name__):
            raise
        raise click.ClickException(f"Failed to apply SHARD: {e}")


@shard.command("test")
@click.argument("worktree_name")
@click.option("--rite", "rite_name", default="test", help="Rite to run (default: test)")
@click.option("--verbose", "-v", is_flag=True, help="Show command output")
@click.pass_context
def shard_test(ctx, worktree_name, rite_name, verbose):
    """
    Run a rite in a SHARD's worktree.

    Runs the specified rite (default: 'test') in the shard's worktree directory.
    The rite must be defined in the project's .skein/rites.yaml.

    Examples:
        skein shard test my-shard-001           # Run 'test' rite
        skein shard test my-shard-001 --rite lint  # Run 'lint' rite
        skein shard test my-shard-001 -v        # Verbose output
    """
    try:
        shard_info = shard_engine.get_shard_status(worktree_name)
        if not shard_info:
            raise click.ClickException(f"SHARD not found: {worktree_name}")

        worktree_path = Path(shard_info["worktree_path"])
        if not worktree_path.exists():
            raise click.ClickException(f"SHARD worktree not found: {worktree_path}")

        # Rites are project-level; shards just run them in their worktree context.
        project_root = shard_engine.get_project_root()
        config = load_rites_config(project_root)
        rites_dict = config.get("rites", {})

        if rite_name not in rites_dict:
            if not rites_dict:
                raise click.ClickException(
                    f"No rites defined. Create {project_root / '.skein' / 'rites.yaml'}"
                )
            available = ", ".join(rites_dict.keys())
            raise click.ClickException(f"Unknown rite: {rite_name}\nAvailable: {available}")

        rite_config = rites_dict[rite_name]

        click.echo(f"▶ Running rite '{rite_name}' in shard: {worktree_name}")
        click.echo(f"  Worktree: {worktree_path}")

        success = run_rite_commands(rite_name, rite_config, worktree_path, verbose)

        if success:
            click.echo(f"✓ Rite '{rite_name}' completed in shard {worktree_name}")
        else:
            raise click.ClickException(f"Rite '{rite_name}' failed in shard {worktree_name}")

    except shard_engine.ShardError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        if "ClickException" in str(type(e).__name__):
            raise
        raise click.ClickException(f"Failed to run rite in shard: {e}")
