"""
SKEIN analytics functions for observability and debugging.
"""

from typing import List, Dict, Any
from collections import defaultdict, Counter


def find_orphaned_threads(threads: List[Dict], folios: List[Dict]) -> List[Dict]:
    """Find threads pointing to non-existent resources.

    Args:
        threads: All threads in SKEIN
        folios: All folios in SKEIN

    Returns:
        List of orphaned thread info dicts
    """
    # Build valid IDs set with defensive get
    valid_ids = set()
    for f in folios:
        if 'folio_id' in f:
            valid_ids.add(f['folio_id'])

    orphaned = []
    for thread in threads:
        # Skip threads missing required fields
        if not all(k in thread for k in ['thread_id', 'from_id', 'to_id', 'type']):
            continue

        thread_id = thread['thread_id']

        if thread['to_id'] not in valid_ids:
            orphaned.append({
                'thread_id': thread_id,
                'direction': 'to_id',
                'missing_id': thread['to_id'],
                'type': thread['type'],
                'weaver': thread.get('weaver'),
                'created_at': thread.get('created_at', 'unknown')
            })

        if thread['from_id'] not in valid_ids:
            orphaned.append({
                'thread_id': thread_id,
                'direction': 'from_id',
                'missing_id': thread['from_id'],
                'type': thread['type'],
                'weaver': thread.get('weaver'),
                'created_at': thread.get('created_at', 'unknown')
            })

    return orphaned


def analyze_by_weaver(threads: List[Dict]) -> Dict[str, Any]:
    """Analyze thread creation by weaver.

    Returns:
        Dict mapping weaver to stats
    """
    by_weaver = defaultdict(lambda: {
        'total': 0,
        'types': Counter()
    })

    for thread in threads:
        # Skip threads missing required fields
        if 'type' not in thread:
            continue

        weaver = thread.get('weaver') or 'null'
        by_weaver[weaver]['total'] += 1
        by_weaver[weaver]['types'][thread['type']] += 1

    # Sort by total (descending)
    sorted_weavers = sorted(
        by_weaver.items(),
        key=lambda x: x[1]['total'],
        reverse=True
    )

    return dict(sorted_weavers)


def analyze_by_type(threads: List[Dict]) -> Dict[str, Any]:
    """Analyze thread type distribution.

    Returns:
        Dict with type counts and breakdowns
    """
    # Count types with defensive get
    type_counts = Counter()
    for t in threads:
        if 'type' in t:
            type_counts[t['type']] += 1

    # Breakdown by content for specific types
    status_values = Counter()
    tag_values = Counter()
    reply_targets = defaultdict(int)

    for thread in threads:
        # Skip threads without type field
        if 'type' not in thread:
            continue

        if thread['type'] == 'status' and thread.get('content'):
            status_values[thread['content']] += 1
        elif thread['type'] == 'tag' and thread.get('content'):
            tag_values[thread['content']] += 1
        elif thread['type'] == 'reply':
            # Extract target type from to_id prefix
            to_id = thread.get('to_id', '')
            target_type = to_id.split('-')[0] if '-' in to_id else 'unknown'
            reply_targets[target_type] += 1

    return {
        'total': len(threads),
        'by_type': dict(type_counts),
        'status_breakdown': dict(status_values),
        'tag_breakdown': dict(tag_values),
        'reply_targets': dict(reply_targets)
    }


def print_orphaned_threads(threads: List[Dict], folios: List[Dict]):
    """Pretty print orphaned threads."""
    import click

    orphaned = find_orphaned_threads(threads, folios)

    if not orphaned:
        click.echo("✅ No orphaned threads found - data integrity looks good!")
        return

    click.echo(f"🔴 ORPHANED THREADS ({len(orphaned)} found):")
    click.echo()

    for item in orphaned[:10]:  # Limit to 10 for readability
        click.echo(f"{item['thread_id']} → {item['missing_id']} (MISSING)")
        click.echo(f"  Type: {item['type']} | Weaver: {item.get('weaver') or 'null'}")
        click.echo(f"  Created: {item['created_at']}")
        click.echo()

    if len(orphaned) > 10:
        click.echo(f"... and {len(orphaned) - 10} more")
        click.echo()

    # Summary stats
    by_direction = Counter(item['direction'] for item in orphaned)
    by_type = Counter(item['type'] for item in orphaned)
    by_weaver = Counter(item.get('weaver') or 'null' for item in orphaned)

    click.echo("Broken by direction:")
    for direction, count in by_direction.most_common():
        click.echo(f"  - {direction} missing: {count} threads")

    click.echo()
    click.echo("Broken by type:")
    for thread_type, count in by_type.most_common():
        click.echo(f"  - {thread_type}: {count} threads")

    click.echo()
    click.echo("Most affected weavers:")
    for weaver, count in by_weaver.most_common(5):
        click.echo(f"  - {weaver}: {count} orphaned threads")

    click.echo()
    click.echo("💡 Action: Review these threads - may indicate deleted resources or migration issues")


def print_weaver_stats(threads: List[Dict]):
    """Pretty print weaver statistics."""
    import click

    stats = analyze_by_weaver(threads)
    total = len(threads)

    click.echo(f"👥 THREAD CREATION BY WEAVER ({total} total threads):")
    click.echo()

    for weaver, data in list(stats.items())[:10]:  # Top 10 weavers
        count = data['total']
        pct = (count / total) * 100

        click.echo(f"{weaver}: {count} threads ({pct:.1f}%)")

        # Show type breakdown
        for thread_type, type_count in data['types'].most_common(5):
            type_pct = (type_count / count) * 100
            click.echo(f"  - {thread_type}: {type_count} ({type_pct:.1f}%)")

        click.echo()


def print_type_distribution(threads: List[Dict]):
    """Pretty print type distribution."""
    import click

    stats = analyze_by_type(threads)
    total = stats['total']

    click.echo(f"📈 THREAD TYPE DISTRIBUTION ({total} threads):")
    click.echo()

    # Sort by count descending
    sorted_types = sorted(
        stats['by_type'].items(),
        key=lambda x: x[1],
        reverse=True
    )

    max_count = max(count for _, count in sorted_types) if sorted_types else 1

    for thread_type, count in sorted_types:
        pct = (count / total) * 100
        bar_length = int((count / max_count) * 20)
        bar = '█' * bar_length

        click.echo(f"{thread_type:12} {count:3} ({pct:4.1f}%)  {bar}")

    # Show breakdowns
    if stats['status_breakdown']:
        click.echo()
        click.echo("Status Values (status threads):")
        for status, count in sorted(stats['status_breakdown'].items(),
                                   key=lambda x: x[1], reverse=True):
            click.echo(f"  - {status}: {count}")

    if stats['tag_breakdown']:
        click.echo()
        click.echo("Common Tags (tag threads):")
        for tag, count in sorted(stats['tag_breakdown'].items(),
                                key=lambda x: x[1], reverse=True)[:10]:
            click.echo(f"  - {tag}: {count}")

    if stats['reply_targets']:
        click.echo()
        click.echo("Reply Targets (reply threads):")
        for target_type, count in sorted(stats['reply_targets'].items(),
                                        key=lambda x: x[1], reverse=True):
            click.echo(f"  - {target_type}: {count} replies")
