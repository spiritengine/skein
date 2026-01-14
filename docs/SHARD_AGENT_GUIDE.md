# SHARD Agent Guide

**SHARD (Simultaneous Harmonized Agent Research & Development)** - Git worktree-based isolation for parallel agent work.

---

## What Are Shards?

Shards provide isolated git worktrees for agents to work in parallel without conflicts. Each shard is:
- A separate working directory (`worktrees/<shard-name>/`)
- An isolated git branch (`shard-<shard-name>`)
- Tracked in SQLite with metadata (base commit, creation time, etc.)

**Why shards?**
- Agents can work independently without blocking each other
- Master branch stays clean until work is reviewed
- Easy to abandon work that doesn't pan out
- Built-in drift detection shows when your work diverged from master

---

## Quick Start

### Basic Workflow

```bash
# 1. Spawn a shard
skein shard spawn my-feature
# Creates: worktrees/my-feature-20260113-001/

# 2. Work in the shard
cd worktrees/my-feature-20260113-001/
# ... make changes ...
git add . && git commit -m "Implement feature"

# 3. Review before merging
skein shard review my-feature-20260113-001
# Shows: your changes, drift from master, conflict status

# 4. Merge to master
skein shard merge my-feature-20260113-001
# Merges and cleans up automatically

# 5. If conflicts occur, create a graft
skein shard graft my-feature-20260113-001
# Creates fresh worktree applying your work onto current master
```

---

## Core Commands

### `skein shard spawn <name>`

Create a new isolated worktree for development.

```bash
# Simple spawn
skein shard spawn fix-bug

# With description
skein shard spawn add-feature --description "Add user preferences"

# Link to a brief
skein shard spawn implement-spec --brief brief-20260113-xyz
```

**What it does:**
- Creates `worktrees/<name>-YYYYMMDD-NNN/` directory
- Creates `shard-<name>-YYYYMMDD-NNN` branch from current master
- Records base commit in SQLite for drift tracking
- Returns path to new worktree

**Output:**
```
Spawned SHARD: fix-bug-20260113-001
  Path: /home/patrick/projects/skein/worktrees/fix-bug-20260113-001
  Branch: shard-fix-bug-20260113-001
```

### `skein shard review <name>`

Deep review of a shard showing drift, conflicts, and merge readiness.

```bash
skein shard review fix-bug-20260113-001
```

**Shows:**
- **Your Work**: Base commit, file changes, line counts
- **Master Activity**: Commits merged since you branched, notable changes
- **Conflict Status**: Whether merge will succeed (requires git 2.38+)
- **Next Actions**: Clear commands to merge or graft

**Example output (no drift):**
```
=== SHARD: fix-bug-20260113-001 ===

Your Work (clean, ready to integrate):
  Base: a1b2c3d (2026-01-13 10:00:00)
  Commits: 2
  Changes: 3 files changed, +150/-25

✓ Master is at same state as your base
✓ Ready to merge

Merge to master:
  → skein shard merge fix-bug-20260113-001
```

**Example output (with drift, no conflicts):**
```
=== SHARD: fix-bug-20260113-001 ===

Your Work (clean, ready to integrate):
  Base: a1b2c3d (2 days ago)
  Commits: 2
  Changes: 3 files changed, +150/-25

Master Activity Since Your Base:
  47 new commits merged to master

  Notable changes:
    - qgun/ directory refactored (-400 lines in baseline)
    - mill/wheel updated

  ✓ Integration test: No conflicts detected
  ✓ Ready to merge onto current master

Merge to master:
  → skein shard merge fix-bug-20260113-001
```

**Example output (conflicts detected):**
```
=== SHARD: fix-bug-20260113-001 ===

Your Work:
  Base: b2c3d4e (3 days ago)
  Commits: 3
  Changes: 5 files changed, +250/-100

Master Activity Since Your Base:
  89 new commits merged to master

  Notable changes:
    - auth.py refactored (may conflict with your changes)
    - config.py structure changed

  ⚠ Integration test: Conflicts detected
    - auth.py: 3 conflict regions
    - config.py: 1 conflict region

Create graft worktree to resolve:
  → skein shard graft fix-bug-20260113-001
```

### `skein shard triage`

Overview of all shards with actionable status.

```bash
skein shard triage
```

**Shows:**
- Status symbol (✓ clean, ⚠ conflicts, needs commit)
- Drift indicator (master +N commits ahead)
- Age and file stats
- Quick actions for each shard

**Example output:**
```
SHARDS (3 total):

  ✓  fix-bug-20260113-001              clean          2 commits         +150/-25
       base: a1b2c3d, master +47 (no conflicts)

  ⚠  big-refactor-20260110-001         conflicts      5 commits         +500/-200
       base: b2c3d4e, master +89 (conflicts in auth.py, config.py)

     needs-work-20260112-001            uncommitted    1 commits         +75/-10
       base: c4d5e6f, uncommitted changes

Commands:
  skein shard review <name>    # View details
  skein shard merge <name>     # Merge to master
  skein shard graft <name>     # Create graft to resolve conflicts
```

### `skein shard merge <name>`

Merge shard to master and clean up.

```bash
skein shard merge fix-bug-20260113-001
```

**What it does:**
1. Tests integration with current master (requires git 2.38+)
2. If clean: merges via `--no-ff` (preserves branch history)
3. If conflicts: suggests graft command instead
4. On success: suggests cleanup command

**Success output:**
```
Testing integration with current master...
✓ Clean integration

Merging to master...
✓ Merged fix-bug-20260113-001 to master
  Commit: m4s5t6e (Fix authentication timeout)

Cleanup worktree:
  → skein shard cleanup fix-bug-20260113-001
```

**Conflict output:**
```
Testing integration with current master...
✗ Conflicts detected
  - auth.py: 3 conflict regions
  - config.py: 1 conflict region

Create graft worktree to resolve:
  → skein shard graft fix-bug-20260113-001
```

### `skein shard graft <name>`

Create a fresh worktree applying your work onto current master.

**When to use:**
- Review shows conflicts
- Merge attempt fails
- Shard is very stale (many commits behind)

```bash
skein shard graft fix-bug-20260113-001
```

**What it does:**
1. Creates new worktree from current master
2. Cherry-picks your commits onto it
3. If conflicts: leaves them for you to resolve
4. If clean: ready to merge immediately

**Creates:** `worktrees/fix-bug-20260113-001-graft/`

**Clean graft output:**
```
Creating graft worktree from current master...
Applying your work...
✓ Applied cleanly (no conflicts)

Graft created at:
  worktrees/fix-bug-20260113-001-graft/

Your work has been applied onto current master.
Review and test, then merge:
  skein shard merge fix-bug-20260113-001-graft
```

**Conflict graft output:**
```
Creating graft worktree from current master...
Applying your work...
✗ Conflicts in: auth.py, config.py

Graft created at:
  worktrees/fix-bug-20260113-001-graft/

Resolve conflicts:
  cd worktrees/fix-bug-20260113-001-graft
  (edit auth.py and config.py to resolve conflicts)
  git add auth.py config.py
  git commit

Then merge:
  skein shard merge fix-bug-20260113-001-graft
```

### `skein shard cleanup <name>`

Remove worktree and branch after merge.

```bash
# Clean up single shard
skein shard cleanup fix-bug-20260113-001

# Clean up entire graft chain
skein shard cleanup fix-bug-20260113-001 --chain
```

**With `--chain` flag:**
Removes all worktrees in the graft lineage:
- `fix-bug-20260113-001` (original)
- `fix-bug-20260113-001-graft`
- `fix-bug-20260113-001-graft-graft` (if master moved again)

**Output:**
```
Tracing worktree chain for: fix-bug-20260113-001

Found chain:
  fix-bug-20260113-001 (original)
  └─ fix-bug-20260113-001-graft

Removing 2 worktrees...
✓ Removed worktrees/fix-bug-20260113-001/
✓ Removed worktrees/fix-bug-20260113-001-graft/
✓ Deleted 2 branches
```

---

## Understanding Drift

**Drift** = commits merged to master since your shard branched.

### Why Drift Matters

When you spawn a shard, SKEIN records the **base commit** (current master HEAD). If other agents merge work while you're developing, master "moves ahead" of your base.

**Not a problem by itself** - drift is normal in active projects. But it affects merging:

- **No drift**: Direct merge, no issues
- **Drift, no conflicts**: Merge applies your work onto current master
- **Drift with conflicts**: Graft needed to resolve overlapping changes

### How SKEIN Shows Drift

**In review:**
```
Master Activity Since Your Base:
  47 new commits merged to master

  Notable changes:
    - qgun/ directory refactored
    - mill/wheel updated

  ✓ Integration test: No conflicts detected
```

**In triage:**
```
✓  fix-bug-20260113-001              clean          2 commits         +150/-25
     base: a1b2c3d, master +47 (no conflicts)
```

**Key phrase:** "Master has continued" (not "you're behind")

This framing removes blame/urgency while conveying the same information.

---

## Graft Workflow

### What is a Graft?

A graft is a new shard that reapplies your work onto current master. Think of it as "rebasing via worktree."

**Graft chain:**
```
fix-bug-20260113-001           (original, base: old master)
  ↓ conflicts with master
fix-bug-20260113-001-graft     (base: current master)
  ↓ master moved again
fix-bug-20260113-001-graft-graft
```

Each `-graft` suffix represents applying work onto a newer master.

### When to Graft

**Automatic suggestion from merge:**
```bash
skein shard merge old-feature
# ✗ Conflicts detected → suggests graft
```

**Proactive grafting:**
```bash
skein shard review old-feature
# Shows master +200 commits ahead
# Consider grafting before attempting merge
```

**After master moves post-graft:**
```bash
# You created a graft, but master moved again before you merged
skein shard graft old-feature-graft  # Creates -graft-graft
```

### Graft Iteration is Normal

If master is very active, you might create multiple grafts:
```
feature-graft         # Monday: master at commit A
feature-graft-graft   # Tuesday: master at commit B
```

This is expected behavior, not a failure. The workflow **normalizes iteration**.

### Merging Grafts

Same as merging original shards:
```bash
skein shard merge fix-bug-20260113-001-graft
```

After merge, clean up the chain:
```bash
skein shard cleanup fix-bug-20260113-001 --chain
```

---

## Common Scenarios

### Scenario 1: Clean Merge (No Drift)

You work quickly, master hasn't moved:

```bash
skein shard spawn quick-fix
cd worktrees/quick-fix-20260113-001
# ... make changes ...
git commit -am "Fix typo"

skein shard review quick-fix-20260113-001
# ✓ Master is at same state as your base

skein shard merge quick-fix-20260113-001
# ✓ Merged
skein shard cleanup quick-fix-20260113-001
```

### Scenario 2: Drift But No Conflicts

You worked for 2 days, others merged 20 commits:

```bash
skein shard review my-feature-20260111-001
# Master Activity: 20 new commits
# ✓ Integration test: No conflicts detected

skein shard merge my-feature-20260111-001
# ✓ Merged onto current master
skein shard cleanup my-feature-20260111-001
```

### Scenario 3: Conflicts Detected

Your changes overlap with recent master changes:

```bash
skein shard review big-change-20260110-001
# ⚠ Conflicts detected in: auth.py, config.py

skein shard graft big-change-20260110-001
# Creates worktree at: big-change-20260110-001-graft/

cd worktrees/big-change-20260110-001-graft
# Resolve conflicts manually
git add auth.py config.py
git commit

skein shard merge big-change-20260110-001-graft
# ✓ Merged
skein shard cleanup big-change-20260110-001 --chain
```

### Scenario 4: Master Moved During Graft Resolution

You created a graft, but master moved again before you finished:

```bash
# Monday: Create graft
skein shard graft old-feature-20260107-001

# Tuesday: Master has new commits
skein shard review old-feature-20260107-001-graft
# Master Activity: 15 new commits
# ⚠ Conflicts detected

# Create graft of graft
skein shard graft old-feature-20260107-001-graft
# Creates: old-feature-20260107-001-graft-graft

cd worktrees/old-feature-20260107-001-graft-graft
# Resolve and merge
```

### Scenario 5: Stale Shard (Very Old)

Your shard is weeks old, master moved significantly:

```bash
skein shard review ancient-work-20251215-001
# Master Activity: 500+ new commits
# Notable changes: [extensive list]

# Decision: Too stale to merge safely
# Options:
# A) Graft and carefully review
skein shard graft ancient-work-20251215-001

# B) Abandon and restart
skein shard cleanup ancient-work-20251215-001
skein shard spawn fresh-attempt
# Cherry-pick good commits manually
```

---

## Other Commands

### `skein shard diff <name>`

Show your actual changes (work diff).

```bash
# Default: work diff (your changes from base)
skein shard diff fix-bug-20260113-001

# Integration diff (what would merge, includes master evolution)
skein shard diff fix-bug-20260113-001 --integration
```

### `skein shard stash <description>`

Save uncommitted changes to a new shard.

```bash
cd /home/patrick/projects/skein
# ... make changes but don't commit ...

skein shard stash "WIP: half-done refactor"
# Creates shard with your uncommitted changes as a commit
```

### `skein shard apply <name>`

Apply shard changes to current branch as uncommitted changes.

```bash
# Copy work from shard to current directory
skein shard apply fix-bug-20260113-001

# Useful for cherry-picking specific changes
```

### `skein shard tender <name>`

Mark shard as ready for QM review.

```bash
skein shard tender my-feature-20260113-001 \
  --summary "Implemented user preferences system" \
  --confidence 8
```

Creates a tender folio visible in QM dashboards.

---

## Git Version Requirements

**Full drift detection requires git 2.38+** for three-argument `merge-tree`.

**What works on older git:**
- Spawn, commit, diff, cleanup (all basic operations)
- Review (shows drift, but conflict detection reports "unknown")
- Merge (attempts merge, relies on git's native conflict detection)

**What's limited:**
- Conflict detection before merge (can't predict conflicts)
- Integration testing (returns "unknown" status)

**Workaround on old git:**
Attempt merge and handle conflicts if they occur:
```bash
skein shard merge my-feature  # Might fail if conflicts
# If fails: manually resolve in worktree, then commit and merge
```

---

## Best Practices

### 1. Commit Early, Commit Often

Small, focused commits make:
- Drift easier to handle (granular changes)
- Grafts more likely to succeed automatically
- Cherry-picking viable if needed

### 2. Review Before Merge

Always run `skein shard review` before merge:
- See drift impact
- Catch conflicts early
- Understand what changed on master

### 3. Graft When Stale

If review shows master +50 commits or more, consider grafting proactively:
```bash
# Proactive graft on stale shard
skein shard graft old-feature
# Resolve conflicts fresh, easier than debugging merge failures
```

### 4. Clean Up After Merge

Delete worktrees after successful merge:
```bash
skein shard cleanup my-feature --chain  # Remove entire graft chain
```

Keeps worktrees directory manageable.

### 5. Use Descriptive Names

Good shard names help future you:
```bash
# Good
skein shard spawn fix-auth-timeout
skein shard spawn add-user-preferences

# Less helpful
skein shard spawn test
skein shard spawn fix
```

---

## Troubleshooting

### "Conflicts detected" but I don't see them

Conflict detection (git 2.38+) predicts conflicts before merge. To see actual conflicts:

```bash
# Create graft to see real conflict markers
skein shard graft my-feature
cd worktrees/my-feature-graft
# Check files for <<<<<<< markers
```

### "Unknown merge status"

Git version < 2.38. Conflict detection unavailable. Try merging anyway:

```bash
# Attempt merge (might fail if conflicts exist)
git checkout master
git merge --no-ff shard-my-feature

# If conflicts: resolve, commit, done
# If clean: merge succeeded
```

Or upgrade git to 2.38+.

### Graft created but still conflicts

Master is moving fast. Options:

1. Resolve conflicts in graft worktree and merge quickly
2. Create another graft (graft-of-graft) to catch latest master
3. Coordinate with other agents to pause master changes briefly

### Lost track of graft chain

```bash
skein shard triage
# Shows graft relationships and chain depth

# Or check SQLite
sqlite3 .skein/shards.db "SELECT worktree_name, parent_worktree FROM shards"
```

### Shard worktree missing but branch exists

Worktree was deleted manually. Clean up branch:

```bash
git branch -D shard-my-feature-20260113-001
```

Or recreate worktree (if work is valuable):

```bash
git worktree add worktrees/my-feature-20260113-001 shard-my-feature-20260113-001
```

---

## Advanced: Shard Metadata

SKEIN stores shard metadata in `.skein/shards.db` (SQLite):

```sql
CREATE TABLE shards (
    worktree_name TEXT PRIMARY KEY,
    parent_worktree TEXT,      -- For graft chains
    base_commit TEXT NOT NULL, -- Where it branched from master
    created_at TIMESTAMP,
    spawned_by TEXT,           -- Agent ID
    brief_id TEXT,
    description TEXT,
    status TEXT,
    confidence INTEGER
);
```

**Querying metadata:**
```bash
sqlite3 .skein/shards.db "SELECT worktree_name, base_commit, created_at FROM shards"
```

**Parent worktree tracking:**
Grafts record their parent, enabling chain detection:
```
my-feature → parent: NULL          (original)
my-feature-graft → parent: my-feature
my-feature-graft-graft → parent: my-feature-graft
```

---

## Summary

**Shards = isolated worktrees for parallel agent work**

**Core workflow:**
1. `spawn` - Create isolated workspace
2. Work - Make changes, commit
3. `review` - Check drift and conflicts
4. `merge` - Integrate to master
5. `cleanup` - Remove worktree

**When master moves:**
- Review shows drift clearly
- Merge handles clean integration automatically
- Graft resolves conflicts by reapplying onto current master

**Remember:**
- Drift is normal, not a failure
- Graft iteration is expected on active projects
- "Master has continued" not "you're behind"

---

For more on SKEIN folio types and agent collaboration, see [SKEIN_AGENT_GUIDE.md](SKEIN_AGENT_GUIDE.md).
