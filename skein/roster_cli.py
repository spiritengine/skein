"""Roster / lifecycle / activity / chain-yield CLI (agent-coordination port, Stage 2).

The daily agent-coordination verbs, re-homed off the legacy client→server HTTP
hop onto the local content-hash :class:`~skein.station.Station`:

- ``ignite`` / ``register`` — mint the agent's ``type=agent`` folio in the
  ``roster`` site and guard its name (the new D1 name register).
- ``ready`` / ``torch`` / ``complete`` — drive the lifecycle FSM
  (orienting → active → retiring → retired). ``complete`` also leaves a chain
  yield when ``SKEIN_CHAIN_ID`` is set (the mill hand-off, design D4).
- ``identify`` — set the agent-identity env value for the shell.
- ``roster`` / ``activity`` — the roster list and the authored-folios activity feed.
- ``chain yields`` / ``chain yield`` — read a chain's sack back.

Wired onto the main ``cli`` group the same way Stage 1 attached the shard group
(:func:`skein.cli`'s ``cli.add_command``). Output is screen-reader plain text
— one item per line, a description then the id — with ``--json`` for tooling.
"""

from __future__ import annotations

import os
from typing import List, Optional

import click

from .station import (
    AgentNameTaken,
    AmbiguousReference,
    IllegalAgentTransition,
    Station,
    UnknownAgent,
    UnknownFolio,
    _short,
)

ENV_AGENT = "SKEIN_AGENT"
ENV_CHAIN = "SKEIN_CHAIN_ID"
ENV_CHAIN_TASK = "SKEIN_CHAIN_TASK"


def _open_station(ctx: click.Context) -> Station:
    return Station(ctx.obj.get("data_dir") if ctx.obj else None)


def _default_agent() -> Optional[str]:
    return os.environ.get(ENV_AGENT) or os.environ.get("SKEIN_AGENT")


def _require_agent(agent: Optional[str]) -> str:
    """The agent name/id from --agent or $SKEIN_AGENT, or a clear error."""
    resolved = agent or _default_agent()
    if not resolved:
        raise click.ClickException(
            "no agent identity — pass --agent NAME or set $SKEIN_AGENT "
            "(run 'ignite' first to get an assigned name)."
        )
    return resolved


def _generate_name(existing: set) -> str:
    """A memorable agent name, avoiding ``existing`` (legacy's generator, reused).

    Falls back to a timestamped id if the legacy generator is unavailable, so a
    fresh station can always ignite even outside a full legacy checkout. The
    fallback carries a random token, NOT just a second-resolution timestamp:
    two cold-start ignites in the same second would otherwise generate the same
    name, and since an auto-name becomes its own agent-id, register_agent would
    read the second as a re-ignite of the first and silently merge the two
    identities rather than rejecting (the disjoint-namespace guard rejects a
    name held under a DIFFERENT agent-id, but two identical auto-names share the
    SAME agent-id and read as a re-ignite). The 64-bit ``token_hex(8)`` makes a
    same-second collision astronomically unlikely rather than guaranteeing none —
    the guarantee is probabilistic, not by-construction. ``existing`` dedups the
    rare in-process repeat, and the caller re-checks the store and retries on the
    guard's rejection, so a collision is recoverable even if it does occur.
    """
    try:
        from skein.utils import generate_agent_name

        return generate_agent_name(existing_names=existing)
    except Exception:
        import secrets
        from datetime import datetime, timezone

        base = "agent-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        while True:
            name = f"{base}-{secrets.token_hex(8)}"
            if name not in existing:
                return name


# --- ignite / register ------------------------------------------------------


@click.command()
@click.option("--message", help="Initial task / mission (recorded on the agent folio).")
@click.option("--mantle", help="Mantle / role name (recorded in agent metadata).")
@click.argument("brief_id", required=False)
@click.option("--name", help="Agent name to claim (default: generated, collision-safe).")
@click.option("--agent", "agent", default=None, help="Agent id (default: $SKEIN_AGENT or the name).")
@click.option("--type", "agent_type", default=None, help="Agent type (e.g. claude-code).")
@click.option("--description", default=None, help="Longer description of focus.")
@click.option("--capabilities", default=None, help="Comma-separated capabilities.")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def ignite(
    ctx: click.Context,
    message: Optional[str],
    mantle: Optional[str],
    brief_id: Optional[str],
    name: Optional[str],
    agent: Optional[str],
    agent_type: Optional[str],
    description: Optional[str],
    capabilities: Optional[str],
    output_json: bool,
) -> None:
    """Begin a session: register on the roster as 'orienting'.

    The lifecycle entry point. Mints the agent's roster folio (status orienting)
    and claims its name. After orienting, run 'ready' to go active.
    """
    caps = [c.strip() for c in capabilities.split(",")] if capabilities else []
    metadata = {
        k: v
        for k, v in {
            "ignited_from": brief_id,
            "mantle": mantle,
            "message": message,
        }.items()
        if v
    }
    with _open_station(ctx) as st:
        # Resolve the brief and the mantle BEFORE registering, so a bad reference
        # fails fast and never leaves an orienting agent folio behind for a brief
        # that does not exist. These are the Stage-3 pull-reads: the brief and the
        # mantle are content the agent absorbs as it comes up.
        brief_folio = None
        if brief_id:
            try:
                brief_folio = st.get_folio(brief_id)
            except AmbiguousReference as e:
                lines = "\n".join("  " + _short(m, 24) for m in e.matches)
                raise click.ClickException(f"{e}\n{lines}")
            if not brief_folio:
                raise click.ClickException(f"no brief for reference {brief_id!r}")
        mantle_folio = None
        if mantle:
            try:
                mantle_folio = st.resolve_mantle(mantle)
            except AmbiguousReference as e:
                lines = "\n".join("  " + _short(m, 24) for m in e.matches)
                raise click.ClickException(f"{e}\n{lines}")
            if not mantle_folio:
                raise click.ClickException(f"no mantle found matching {mantle!r}")
        brief_hash = brief_folio["content_hash"] if brief_folio else None

        agent_id = agent or _default_agent() or name
        # Auto-name only when the caller supplied nothing to identify by. The name
        # is generated and bound below; the guarded register is the source of truth
        # for collisions, so an auto-name that loses a race (another ignite claimed
        # it between our snapshot and the register) is regenerated and retried —
        # the point-in-time snapshot in _generate_name is an optimization, not the
        # guarantee.
        auto_named = not name and not agent_id
        attempts = 0
        while True:
            if auto_named:
                existing = {a["name"] for a in st.list_agents()}
                name = _generate_name(existing)
                agent_id = name
            agent_id = agent_id or name
            try:
                folio_hash = st.register_agent(
                    agent_id=agent_id,
                    name=name or agent_id,
                    agent_type=agent_type,
                    description=description or message,
                    capabilities=caps,
                    metadata=metadata,
                    ignited_from=brief_hash,
                )
                break
            except AgentNameTaken as e:
                attempts += 1
                if not auto_named or attempts >= 5:
                    raise click.ClickException(str(e))
                # Auto-name collided with a concurrent register; regenerate.
                continue
        resolved_name = name or agent_id
        # The brief-handoff trail (the ignited_from agent → brief edge) was written
        # atomically inside register_agent via ignited_from=brief_hash, so a
        # registered agent always carries its readable handoff edge.

        # Assemble the mission the agent pulls at birth — brief, then mantle, then
        # the ad-hoc message — each under its own labelled section (the live legacy
        # ignite_start shape).
        mission_parts: List[str] = []
        if brief_folio:
            mission_parts.append(
                f"From brief ({brief_id}):\n{brief_folio.get('content') or ''}"
            )
        if mantle_folio:
            mission_parts.append(
                f"From mantle ({mantle}):\n{mantle_folio.get('content') or ''}"
            )
        if message:
            mission_parts.append(f"Initial task:\n{message}")
        mission = "\n\n".join(mission_parts) if mission_parts else None

        if output_json:
            _emit_json(
                {
                    "agent_id": agent_id,
                    "name": resolved_name,
                    "content_hash": folio_hash,
                    "status": st.status_of(folio_hash),
                    "mission": mission,
                    "brief_id": brief_id,
                    "mantle": mantle,
                }
            )
            return
        click.echo("=" * 60)
        click.echo("IGNITION — Orientation Phase")
        click.echo("=" * 60)
        click.echo()
        if mission:
            if brief_id:
                click.echo(f"Brief: {brief_id}")
            if mantle:
                click.echo(f"Mantle: {mantle}")
            click.echo()
            click.echo("Mission:")
            click.echo(mission)
            click.echo()
        click.echo(f"You are: {resolved_name}")
        click.echo()
        click.echo("After orienting, activate with:")
        click.echo(f"  skein ready --agent {resolved_name}")


@click.command()
@click.argument("agent_id")
@click.option("--name", help="Human-readable name (default: AGENT_ID).")
@click.option("--type", "agent_type", default=None, help="Agent type.")
@click.option("--description", default=None, help="Longer description.")
@click.option("--capabilities", default=None, help="Comma-separated capabilities.")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def register(
    ctx: click.Context,
    agent_id: str,
    name: Optional[str],
    agent_type: Optional[str],
    description: Optional[str],
    capabilities: Optional[str],
    output_json: bool,
) -> None:
    """Register AGENT_ID on the roster (mint its agent folio + guard its name)."""
    caps = [c.strip() for c in capabilities.split(",")] if capabilities else []
    with _open_station(ctx) as st:
        try:
            folio_hash = st.register_agent(
                agent_id=agent_id,
                name=name or agent_id,
                agent_type=agent_type,
                description=description,
                capabilities=caps,
            )
        except AgentNameTaken as e:
            raise click.ClickException(str(e))
        if output_json:
            _emit_json({"agent_id": agent_id, "name": name or agent_id, "content_hash": folio_hash})
        else:
            click.echo(f"Registered: {name or agent_id} {folio_hash}")


@click.command()
@click.argument("agent_id")
@click.option("--eval", "eval_mode", is_flag=True, help="Print an eval-able export line.")
@click.pass_context
def identify(ctx: click.Context, agent_id: str, eval_mode: bool) -> None:
    """Set your agent identity for this shell session.

    Usage: eval $(skein identify NAME --eval)
    """
    if eval_mode:
        click.echo(f"export {ENV_AGENT}={agent_id}")
        return
    click.echo(f"To identify as {agent_id}, run:")
    click.echo(f"  export {ENV_AGENT}={agent_id}")


# --- lifecycle transitions --------------------------------------------------


def _transition(ctx: click.Context, agent: Optional[str], verb: str) -> None:
    """Drive one FSM edge, mapping the station's errors to clean CLI errors."""
    ref = _require_agent(agent)
    mover = {"ready": "agent_ready", "torch": "agent_torch"}[verb]
    with _open_station(ctx) as st:
        try:
            pre_hash = st._resolve_agent(ref)
        except UnknownAgent as e:
            raise click.ClickException(str(e) + " — run 'ignite' first.")
        except AmbiguousReference as e:
            lines = "\n".join("  " + _short(m, 24) for m in e.matches)
            raise click.ClickException(f"{e}\n{lines}")
        # Weave the status thread under the agent-id (the folio's created_by, the
        # roster join key), not the raw ref, which may be the human name. The
        # agent-id is incarnation-stable across a re-ignite, so a pre-lock read of
        # it is safe.
        agent_id = (st.store.get_folio(pre_hash) or {}).get("created_by") or ref
        try:
            # The mover re-resolves UNDER the lock and returns the LIVE folio hash.
            # A concurrent same-name re-ignite could supersede pre_hash between our
            # resolve and the transaction, so report status from the hash the
            # transition actually moved — never the stale pre-resolve (finding C).
            live_hash = getattr(st, mover)(ref, by=agent_id)
        except IllegalAgentTransition as e:
            raise click.ClickException(str(e))
        except UnknownAgent as e:
            # The under-lock re-resolve can also fail: a concurrent succession may
            # have unbound the slug, or made the name ambiguous, between the
            # pre-resolve and the locked move. Map both to the same clean errors as
            # the pre-resolve, never an uncaught traceback (finding from r5).
            raise click.ClickException(str(e) + " — run 'ignite' first.")
        except AmbiguousReference as e:
            lines = "\n".join("  " + _short(m, 24) for m in e.matches)
            raise click.ClickException(f"{e}\n{lines}")
        new_status = st.status_of(live_hash)
    click.echo(f"{ref}: {new_status}")


@click.command()
@click.option("--agent", default=None, help="Agent name (default: $SKEIN_AGENT).")
@click.pass_context
def ready(ctx: click.Context, agent: Optional[str]) -> None:
    """Activate: orienting → active."""
    _transition(ctx, agent, "ready")


@click.command()
@click.option("--agent", default=None, help="Agent name (default: $SKEIN_AGENT).")
@click.pass_context
def torch(ctx: click.Context, agent: Optional[str]) -> None:
    """Begin retirement: active → retiring. File remaining work, then 'complete'."""
    _transition(ctx, agent, "torch")


@click.command()
@click.option("--agent", default=None, help="Agent name (default: $SKEIN_AGENT).")
@click.option("--summary", default=None, help="Optional retirement summary.")
@click.option(
    "--yield-status",
    "yield_status",
    type=click.Choice(["complete", "partial", "blocked"]),
    default=None,
    help="Chain yield status (only used when $SKEIN_CHAIN_ID is set).",
)
@click.option("--yield-outcome", "yield_outcome", default=None, help="What was accomplished.")
@click.option("--yield-notes", "yield_notes", default=None, help="Notes for the next task.")
@click.pass_context
def complete(
    ctx: click.Context,
    agent: Optional[str],
    summary: Optional[str],
    yield_status: Optional[str],
    yield_outcome: Optional[str],
    yield_notes: Optional[str],
) -> None:
    """Retire: retiring → retired. Stores a chain yield if $SKEIN_CHAIN_ID is set."""
    ref = _require_agent(agent)
    chain_id = os.environ.get(ENV_CHAIN)
    task_id = os.environ.get(ENV_CHAIN_TASK)
    with _open_station(ctx) as st:
        # The whole atomic hand-off (transition + chain yield + summary in one
        # transaction, findings A/C) lives in Station.complete_agent so the
        # guarantee is available and testable at the service layer, not just here.
        # The CLI keeps only env/option plumbing and error→ClickException mapping.
        try:
            _live_hash, new_status, sack_id = st.complete_agent(
                ref,
                chain_id=chain_id,
                task_id=task_id,
                yield_status=yield_status,
                yield_outcome=yield_outcome,
                yield_notes=yield_notes,
                summary=summary,
            )
        except UnknownAgent as e:
            raise click.ClickException(str(e) + " — run 'ignite' first.")
        except AmbiguousReference as e:
            lines = "\n".join("  " + _short(m, 24) for m in e.matches)
            raise click.ClickException(f"{e}\n{lines}")
        except IllegalAgentTransition as e:
            raise click.ClickException(str(e))

    if sack_id:
        click.echo(f"Yield stored: {sack_id}")
    click.echo(f"{ref}: {new_status}")


# --- intranet: reply / comment ----------------------------------------------


@click.command()
@click.argument("resource")
@click.argument("message")
@click.option("--agent", default=None, help="Author (default: $SKEIN_AGENT).")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def reply(
    ctx: click.Context,
    resource: str,
    message: str,
    agent: Optional[str],
    output_json: bool,
) -> None:
    """Comment on a resource: thread a reply from you to RESOURCE.

    Works on any folio — an issue, brief, finding, notion. The reply is readable
    in the resource's thread-tree (run 'folio RESOURCE' or 'thread RESOURCE').

      skein reply issue-123 "I'll take this" --agent nub-0622
    """
    author = _require_agent(agent)
    with _open_station(ctx) as st:
        try:
            thread_hash = st.reply(resource, message, by=author)
        except AmbiguousReference as e:
            lines = "\n".join("  " + _short(m, 24) for m in e.matches)
            raise click.ClickException(f"{e}\n{lines}")
        except UnknownFolio:
            raise click.ClickException(f"no folio for reference {resource!r}")
    if output_json:
        _emit_json({"thread_hash": thread_hash, "resource": resource, "author": author})
        return
    click.echo(f"Posted reply: {_short(thread_hash)}")


# --- roster / activity ------------------------------------------------------


@click.command()
@click.option("--status", default=None, help="Filter by lifecycle status.")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def roster(ctx: click.Context, status: Optional[str], output_json: bool) -> None:
    """List registered agents (one per name) with their lifecycle status."""
    with _open_station(ctx) as st:
        agents = st.list_agents(status=status)
        if output_json:
            _emit_json(
                [
                    {
                        "name": a["name"],
                        "agent_id": a.get("created_by"),
                        "status": a["status"],
                        "agent_type": (a.get("meta") or {}).get("agent_type"),
                        "content_hash": a["content_hash"],
                    }
                    for a in agents
                ]
            )
            return
        if not agents:
            click.echo("no agents registered")
            return
        for a in agents:
            atype = (a.get("meta") or {}).get("agent_type")
            type_part = f" [{atype}]" if atype else ""
            click.echo(f"{a['name']} — {a['status']}{type_part} {_short(a['content_hash'])}")


@click.command()
@click.option("--status", default=None, help="Filter by lifecycle status.")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def activity(ctx: click.Context, status: Optional[str], output_json: bool) -> None:
    """The roster activity feed: each agent's authored-folio activity, recent first."""
    with _open_station(ctx) as st:
        feed = st.agent_activity(status=status)
        if output_json:
            _emit_json(feed)
            return
        if not feed:
            click.echo("no agents registered")
            return
        for a in feed:
            site_part = f", site {a['working_site']}" if a["working_site"] else ""
            click.echo(
                f"{a['name']} — {a['status']}, {a['activity_status']}, "
                f"{a['folio_count']} folios{site_part} {_short(a['content_hash'])}"
            )


# --- chain yields -----------------------------------------------------------


@click.group()
def chain() -> None:
    """Read a chain's sack (the mill hand-off yields)."""


@chain.command("yields")
@click.argument("chain_id")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def chain_yields(ctx: click.Context, chain_id: str, output_json: bool) -> None:
    """List all yields in CHAIN_ID, in execution order."""
    with _open_station(ctx) as st:
        items = st.chain_yields(chain_id)
        if output_json:
            _emit_json(items)
            return
        if not items:
            click.echo(f"no yields for chain {chain_id}")
            return
        for y in items:
            click.echo(
                f"{y.get('status') or '?'}: {y.get('outcome') or ''} "
                f"(task {y.get('task_id')}, by {y.get('agent_id')}) {y['sack_id']}"
            )


@chain.command("yield")
@click.argument("sack_id")
@click.option("--json", "output_json", is_flag=True)
@click.pass_context
def chain_yield(ctx: click.Context, sack_id: str, output_json: bool) -> None:
    """Show one yield by its SACK_ID."""
    with _open_station(ctx) as st:
        y = st.chain_yield(sack_id)
        if not y:
            raise click.ClickException(f"no yield {sack_id!r}")
        if output_json:
            _emit_json(y)
            return
        click.echo(f"{y['sack_id']}  chain {y.get('chain_id')}  task {y.get('task_id')}")
        if y.get("agent_id"):
            click.echo(f"by {y['agent_id']}")
        if y.get("status"):
            click.echo(f"status: {y['status']}")
        if y.get("outcome"):
            click.echo(f"outcome: {y['outcome']}")
        if y.get("artifacts"):
            click.echo("artifacts: " + ", ".join(y["artifacts"]))
        if y.get("notes"):
            click.echo(f"notes: {y['notes']}")


def _emit_json(value) -> None:
    import json as _json

    click.echo(_json.dumps(value, indent=2, ensure_ascii=False, default=str))


COMMANDS: List[click.Command] = [
    ignite,
    register,
    identify,
    ready,
    torch,
    complete,
    reply,
    roster,
    activity,
    chain,
]
