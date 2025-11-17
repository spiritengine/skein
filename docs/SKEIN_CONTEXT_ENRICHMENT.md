# SKEIN Context Enrichment Documentation

**Created:** 2025-11-14
**Purpose:** Enrich SKEIN folios with conversation context for agent succession and knowledge archaeology

---

## Overview

The SKEIN Context Enrichment system transforms SKEIN from a simple knowledge repository into a **context time machine** by enriching folios with detailed snapshots of the conversation state at the moment they were created. This enables:

- **Agent Succession**: Next agent can see exactly what the previous agent had done
- **Archaeological Analysis**: Understand what information informed decisions
- **Pattern Mining**: Discover effective investigation workflows
- **Knowledge Graphs**: Build connections between folios, conversations, and code

## Quick Start

### Basic Usage

```bash
# Scan a strand for SKEIN calls
make skein-scan STRAND=strands/2025-11-12_14-30-45_cc_ae652582

# Enrich folios from a single strand
make skein-enrich STRAND=strands/2025-11-12_14-30-45_cc_ae652582

# Batch enrich recent strands
make skein-enrich-batch

# Verify a folio has been enriched
make skein-enrich-verify FOLIO_ID=brief-20251112-g5ly

# Show enrichment statistics
make skein-stats
```

### What Gets Enriched

When a SKEIN folio is enriched, it receives a `context_snapshot` field containing:

```json
{
  "context_snapshot": {
    "strand_id": "2025-11-12_14-30-45_cc_ae652582",
    "strand_path": "strands/2025-11-12_14-30-45_cc_ae652582",
    "message_position": 450,
    "total_messages": 500,
    "conversation_start": "2025-11-12T14:30:45Z",
    "conversation_at_post": "2025-11-12T18:30:00Z",
    "conversation_duration": "~4.0 hours",
    "conversation_depth": "500 messages, ~4.0 hours",
    "todos_completed": [
      "Check existing strand format",
      "Implement tool extraction"
    ],
    "todos_pending": [
      "Test with strategic conversation"
    ],
    "todos_in_progress": [
      "Add tool_execution memory entries"
    ],
    "files_accessed": [
      "scripts/parse_cc_conversation.py",
      "Makefile",
      "strand.py"
    ],
    "files_count": 12,
    "recent_work_summary": "Implementing tool conversion with 1MB truncation",
    "tools_used": {
      "Read": 12,
      "Edit": 8,
      "Bash": 5,
      "Grep": 3
    },
    "tool_sequence": ["Read", "Edit", "Bash", "Read", "Edit"],
    "total_tool_calls": 28,
    "enriched_at": "2025-11-14T18:00:00Z",
    "enrichment_source": "cc_strand_analysis",
    "enrichment_version": "1.0"
  }
}
```

## Architecture

### Components

1. **Strand Scanner** (`scripts/scan_strand_for_skein.py`)
   - Scans converted CC strands for SKEIN commands
   - Detects folio types (brief, issue, finding, etc.)
   - Extracts context snapshots at posting time
   - Handles multiple detection methods (tool calls, user speech, assistant responses)

2. **SKEIN Enricher** (`scripts/enrich_skein_folios.py`)
   - Loads folios from SKEIN storage
   - Adds context snapshots to folios
   - Creates backups before modification
   - Handles both old and new SKEIN structures

3. **Test Pipeline** (`scripts/test_enrichment_pipeline.py`)
   - Creates test strands with SKEIN calls
   - Demonstrates full enrichment flow
   - Verifies enrichment worked
   - Cleans up test data

### Data Flow

```
1. Claude Code Conversation
         ↓
2. Convert to Strand (existing)
         ↓
3. Scan for SKEIN Calls
         ↓
4. Extract Context Snapshots
         ↓
5. Enrich SKEIN Folios
         ↓
6. Enriched Folios Available
```

## Use Cases

### Agent Succession

**Before enrichment:**
```
Agent A: Posts brief, finishes session
Agent B: Reads brief, starts from scratch
```

**After enrichment:**
```
Agent A: Posts brief with context snapshot
Agent B: Can see:
  - What files Agent A examined
  - What TODOs were completed/pending
  - How long Agent A worked
  - What tools Agent A used
Agent B: Continues from exact context
```

### Decision Archaeology

With enriched folios, you can answer:
- "What files were examined when decision X was made?"
- "What investigation pattern led to discovery Y?"
- "How long from problem identification to solution?"
- "What was the agent's state when they posted this?"

### Pattern Mining

Analyze enriched folios to discover:
- Common investigation workflows (Read → Grep → Edit → Test)
- File clusters (what's examined together)
- Tool usage patterns
- Successful approaches

## Implementation Details

### Scanner Detection Methods

The scanner uses multiple methods to find SKEIN calls:

1. **Tool Call Detection** (best)
   - Looks for `tool_name: "Bash"` with SKEIN commands
   - Most accurate for properly structured strands

2. **User Speech Detection**
   - Searches for `[Tool Result]` containing SKEIN output
   - Works for imported strands without tool metadata

3. **Assistant Response Detection**
   - Finds SKEIN commands in code blocks
   - Catches commands about to be run

### Folio ID Extraction

The enricher extracts folio IDs using patterns:
- `brief-20251112-g5ly` (standard format)
- `Created brief: ID` (creation output)
- `HANDOFF: ID` (handoff format)

### Context Extraction

For each SKEIN call, the scanner extracts:

- **TODO State**: Replays TodoWrite calls to reconstruct state
- **Files Accessed**: Tracks Read/Edit/Write tool calls
- **Recent Work**: Last N assistant responses
- **Tool Usage**: Counts and sequences of tools used
- **Timing**: Duration and message counts

## Makefile Targets

```bash
# Scan and enrich single strand
make skein-scan STRAND=path/to/strand
make skein-enrich STRAND=path/to/strand

# Batch operations
make skein-enrich-batch              # Recent 10 strands
make skein-enrich-batch LIMIT=50     # Recent 50 strands
make skein-enrich-all                 # All from last 7 days

# Verification and stats
make skein-enrich-verify FOLIO_ID=brief-20251112-abc1
make skein-stats                     # Show enrichment progress
```

## Testing

Run the test pipeline to verify everything works:

```bash
# Run full test
python scripts/test_enrichment_pipeline.py

# Clean up test data
python scripts/test_enrichment_pipeline.py --clean
```

The test:
1. Creates a test strand with SKEIN calls
2. Creates matching test folios
3. Runs the enrichment pipeline
4. Verifies enrichment worked
5. Shows enriched context

## Troubleshooting

### No SKEIN Calls Found

If scanner finds no SKEIN calls:
- Check strand has `memory.jsonl` file
- Verify SKEIN commands are present (`grep -i skein`)
- Check detection methods match your strand format

### Folios Not Found

If enricher can't find folios:
- Check folio exists in `.skein/data/sites/*/folios/`
- Verify folio ID format matches
- Check site name is correct

### Enrichment Fails

If enrichment fails:
- Check write permissions on folio files
- Verify JSON format is valid
- Check disk space available

## Limitations

1. **ID Extraction**: Relies on patterns in tool results
2. **TODO State**: Only works if TodoWrite calls preserved
3. **File Tracking**: Only tracks files accessed via tools
4. **Performance**: Large strands (1000+ messages) may be slow

## Future Enhancements

### Phase 2: Rehydration API
```python
context = rehydrate_from_folio("brief-20251112-g5ly")
for file in context.files:
    read(file)  # Load into context
```

### Phase 3: Pattern Recognition
```python
patterns = find_patterns(metric='time_to_solution')
# Output: Read → Grep → Edit (35% of fast solutions)
```

### Phase 4: Context-Aware Search
```bash
skein search --file-accessed authentication.py
skein search --conversation-depth "> 4 hours"
```

## Value Proposition

Every SKEIN post becomes a **time capsule** with full context:

- **WHO**: Which agent created it
- **WHEN**: How far into the conversation
- **WHAT**: What work was done (TODOs)
- **WHERE**: What files were examined
- **HOW**: What tools and patterns used
- **WHY**: What problem was being solved

This transforms SKEIN from "what was said" to "what was happening" - enabling true agent succession and knowledge archaeology.

## Related Documentation

- `implementia/skein_context_enrichment_brief.md` - Original design brief
- `docs/SKEIN_QUICK_START.md` - SKEIN usage guide
- `docs/ARCHITECTURE.md` - System architecture
- `scripts/parse_cc_conversation.py` - CC to strand converter

---

**Status:** ✅ Implemented and Operational
**Version:** 1.0
**Last Updated:** 2025-11-14