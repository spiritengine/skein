# SKEIN Agent Instructions

## Where to Start

If you receive a HANDOFF ID (e.g., "HANDOFF: brief-20251107-z3dc"), ignite with:

```bash
skein ignite BRIEF_ID
```

This auto-registers you, shows context, and creates a succession thread.

## Agent Lifecycle

Every SKEIN session follows this arc:

**START**
1. `skein ignite <brief-id>` to begin orientation process
2. Orient - Read docs, understand context

**WORK**
3. Post folios as you go (issues, findings, frictions)
4. Do the actual work
5. File frictions when small issues arise

**END**
6. `skein torch` - Begin retirement (prompts for reflection)
7. File remaining work (briefs for handoffs, close completed items)
8. `skein complete` - Retire from roster

If directly asked to "torch" by a user, that is an indication to begin this process.

## The SKEIN

Work in this project is managed through the SKEIN, which allows for inter-agent collaboration, knowledge sharing, and logging.

### Key Commands

```bash
# Check your inbox for messages and briefs
skein inbox

# Create a site (workspace for a topic) - do this first if no site exists
skein site create <site-id> "Purpose of this site"

# Post a finding to a site
skein finding <site-id> "What you found"

# Post an issue
skein issue <site-id> "Problem description"

# Post a friction (small issues, poor agent experience)
skein friction <site-id> "What went wrong"

# Create a handoff brief for the next agent
skein brief create <site-id> "Context and next steps"
```

### When to Post

- **Findings**: Discoveries, insights, completed work
- **Issues**: Bugs, problems that need attention
- **Frictions**: Small annoyances (unclear docs, confusing APIs, commands that need rerunning)
- **Briefs**: Handoffs to future agents with context and next steps
