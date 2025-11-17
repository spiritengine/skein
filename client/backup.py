"""
SKEIN Backup & Recovery System

Provides backup and restore functionality for SKEIN data:
- Full backups (tar.gz of entire data directory)
- Verification (checksums)
- Restore with dry-run and confirmation
"""

import os
import json
import tarfile
import hashlib
import shutil
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List


class BackupManager:
    """Manages SKEIN backup and restore operations."""

    def __init__(self, data_dir: Path, backup_dir: Optional[Path] = None):
        """
        Initialize backup manager.

        Args:
            data_dir: Path to .skein/data directory
            backup_dir: Path to store backups (default: ~/.skein/backups)
        """
        self.data_dir = Path(data_dir)
        self.backup_dir = Path(backup_dir) if backup_dir else Path.home() / '.skein' / 'backups'

        # Ensure backup directories exist
        self.full_backup_dir = self.backup_dir / 'full'
        self.full_backup_dir.mkdir(parents=True, exist_ok=True)

    def _calculate_checksum(self, file_path: Path) -> str:
        """Calculate SHA256 checksum of a file."""
        sha256 = hashlib.sha256()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                sha256.update(chunk)
        return sha256.hexdigest()

    def _get_dir_stats(self, directory: Path) -> Dict[str, Any]:
        """Get statistics for a directory."""
        stats = {
            'total_files': 0,
            'total_size': 0,
            'file_types': {}
        }

        for file_path in directory.rglob('*'):
            if file_path.is_file():
                stats['total_files'] += 1
                stats['total_size'] += file_path.stat().st_size
                ext = file_path.suffix or 'no_ext'
                stats['file_types'][ext] = stats['file_types'].get(ext, 0) + 1

        return stats

    def create_full_backup(self, tag: Optional[str] = None) -> Dict[str, Any]:
        """
        Create a full backup of the SKEIN data directory.

        Args:
            tag: Optional tag to append to backup name

        Returns:
            Dict with backup details (path, checksum, stats)
        """
        if not self.data_dir.exists():
            raise ValueError(f"Data directory does not exist: {self.data_dir}")

        # Generate backup filename
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d_%H-%M-%S')
        backup_name = f"skein_full_{timestamp}"
        if tag:
            backup_name += f"_{tag}"
        backup_name += ".tar.gz"

        backup_path = self.full_backup_dir / backup_name

        # Create tar.gz archive
        with tarfile.open(backup_path, 'w:gz', compresslevel=6) as tar:
            # Add data directory contents
            for item in self.data_dir.iterdir():
                tar.add(item, arcname=item.name)

        # Calculate checksum
        checksum = self._calculate_checksum(backup_path)

        # Get backup stats
        backup_size = backup_path.stat().st_size
        source_stats = self._get_dir_stats(self.data_dir)

        # Create metadata file
        metadata = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'backup_name': backup_name,
            'checksum': checksum,
            'backup_size': backup_size,
            'source_dir': str(self.data_dir),
            'source_stats': source_stats,
            'tag': tag,
            'skein_version': '1.0'  # TODO: Get from actual version
        }

        # Remove .tar.gz and add .json for metadata file
        metadata_path = self.full_backup_dir / (backup_name.replace('.tar.gz', '') + '.json')
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2, default=str)

        return {
            'backup_path': str(backup_path),
            'metadata_path': str(metadata_path),
            'backup_name': backup_name,
            'checksum': checksum,
            'backup_size': backup_size,
            'source_stats': source_stats
        }

    def list_backups(self, backup_type: str = 'all') -> List[Dict[str, Any]]:
        """
        List available backups.

        Args:
            backup_type: 'full', 'incremental', or 'all'

        Returns:
            List of backup metadata dicts, sorted by date (newest first)
        """
        backups = []

        if backup_type in ('full', 'all'):
            for metadata_file in self.full_backup_dir.glob('*.json'):
                try:
                    with open(metadata_file) as f:
                        metadata = json.load(f)
                    metadata['type'] = 'full'
                    # Check if actual backup file exists
                    # metadata_file is name.json, backup is name.tar.gz
                    backup_file = metadata_file.parent / (metadata_file.stem + '.tar.gz')
                    metadata['exists'] = backup_file.exists()
                    backups.append(metadata)
                except Exception as e:
                    # Skip invalid metadata files
                    pass

        # Sort by timestamp, newest first
        backups.sort(key=lambda x: x.get('timestamp', ''), reverse=True)

        return backups

    def get_backup(self, backup_id: str) -> Optional[Dict[str, Any]]:
        """
        Get backup metadata by ID (backup name without extension).

        Args:
            backup_id: Backup identifier (e.g., 'skein_full_2025-11-15_00-00-00')

        Returns:
            Backup metadata dict or None if not found
        """
        # Try full backups
        metadata_path = self.full_backup_dir / f"{backup_id}.json"
        if not metadata_path.exists():
            # Try with .tar.gz extension stripped
            if backup_id.endswith('.tar.gz'):
                backup_id = backup_id[:-7]
                metadata_path = self.full_backup_dir / f"{backup_id}.json"

        if metadata_path.exists():
            try:
                with open(metadata_path) as f:
                    metadata = json.load(f)
                metadata['type'] = 'full'
                # metadata_path is name.json, backup is name.tar.gz
                backup_file = metadata_path.parent / (metadata_path.stem + '.tar.gz')
                metadata['exists'] = backup_file.exists()
                metadata['backup_file'] = str(backup_file)
                return metadata
            except Exception:
                pass

        return None

    def verify_backup(self, backup_id: str) -> Dict[str, Any]:
        """
        Verify backup integrity.

        Args:
            backup_id: Backup identifier

        Returns:
            Dict with verification results
        """
        metadata = self.get_backup(backup_id)
        if not metadata:
            return {
                'valid': False,
                'error': f'Backup not found: {backup_id}'
            }

        backup_path = Path(metadata['backup_file'])
        if not backup_path.exists():
            return {
                'valid': False,
                'error': f'Backup file missing: {backup_path}'
            }

        # Verify checksum
        actual_checksum = self._calculate_checksum(backup_path)
        expected_checksum = metadata.get('checksum')

        if actual_checksum != expected_checksum:
            return {
                'valid': False,
                'error': f'Checksum mismatch: expected {expected_checksum}, got {actual_checksum}'
            }

        # Try to read the archive
        try:
            with tarfile.open(backup_path, 'r:gz') as tar:
                members = tar.getnames()
        except Exception as e:
            return {
                'valid': False,
                'error': f'Failed to read archive: {e}'
            }

        return {
            'valid': True,
            'checksum': actual_checksum,
            'file_count': len(members),
            'backup_size': backup_path.stat().st_size
        }

    def restore_backup(
        self,
        backup_id: str,
        dry_run: bool = False,
        confirm: bool = False
    ) -> Dict[str, Any]:
        """
        Restore from a backup.

        Args:
            backup_id: Backup identifier
            dry_run: If True, show what would be restored without making changes
            confirm: Must be True to actually perform restore

        Returns:
            Dict with restore results
        """
        metadata = self.get_backup(backup_id)
        if not metadata:
            return {
                'success': False,
                'error': f'Backup not found: {backup_id}'
            }

        backup_path = Path(metadata['backup_file'])
        if not backup_path.exists():
            return {
                'success': False,
                'error': f'Backup file missing: {backup_path}'
            }

        # Verify backup first
        verification = self.verify_backup(backup_id)
        if not verification['valid']:
            return {
                'success': False,
                'error': f"Backup verification failed: {verification.get('error')}"
            }

        # Get list of files in backup
        with tarfile.open(backup_path, 'r:gz') as tar:
            members = tar.getnames()

        if dry_run:
            return {
                'success': True,
                'dry_run': True,
                'would_restore': {
                    'files': len(members),
                    'source_stats': metadata.get('source_stats', {}),
                    'to_directory': str(self.data_dir),
                    'members': members[:20]  # Show first 20 files
                }
            }

        if not confirm:
            return {
                'success': False,
                'error': 'Restore requires --confirm flag. Use --dry-run to preview.'
            }

        # Create backup of current state before restore
        pre_restore_backup = None
        if self.data_dir.exists() and any(self.data_dir.iterdir()):
            try:
                pre_restore = self.create_full_backup(tag='pre-restore')
                pre_restore_backup = pre_restore['backup_name']
            except Exception as e:
                return {
                    'success': False,
                    'error': f'Failed to backup current state before restore: {e}'
                }

        # Clear existing data directory
        if self.data_dir.exists():
            for item in self.data_dir.iterdir():
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
        else:
            self.data_dir.mkdir(parents=True, exist_ok=True)

        # Extract backup
        try:
            with tarfile.open(backup_path, 'r:gz') as tar:
                tar.extractall(self.data_dir)
        except Exception as e:
            return {
                'success': False,
                'error': f'Failed to extract backup: {e}',
                'pre_restore_backup': pre_restore_backup
            }

        return {
            'success': True,
            'restored_from': metadata['backup_name'],
            'restored_to': str(self.data_dir),
            'files_restored': len(members),
            'pre_restore_backup': pre_restore_backup
        }

    def cleanup_old_backups(
        self,
        keep_last: Optional[int] = None,
        older_than_days: Optional[int] = None,
        dry_run: bool = False
    ) -> Dict[str, Any]:
        """
        Remove old backups based on retention policy.

        Args:
            keep_last: Keep only the N most recent backups
            older_than_days: Remove backups older than N days
            dry_run: Show what would be removed without actually removing

        Returns:
            Dict with cleanup results
        """
        backups = self.list_backups(backup_type='full')

        to_remove = []
        to_keep = []

        if keep_last:
            to_keep = backups[:keep_last]
            to_remove = backups[keep_last:]
        elif older_than_days:
            cutoff = datetime.now(timezone.utc).timestamp() - (older_than_days * 86400)
            for backup in backups:
                try:
                    backup_time = datetime.fromisoformat(backup['timestamp'].replace('Z', '+00:00'))
                    if backup_time.timestamp() < cutoff:
                        to_remove.append(backup)
                    else:
                        to_keep.append(backup)
                except Exception:
                    to_keep.append(backup)  # Keep if can't parse date
        else:
            return {
                'success': False,
                'error': 'Must specify --keep-last or --older-than'
            }

        removed = []
        errors = []

        if not dry_run:
            for backup in to_remove:
                try:
                    backup_name = backup['backup_name']
                    backup_path = self.full_backup_dir / backup_name
                    metadata_path = backup_path.with_suffix('.json')

                    if backup_path.exists():
                        backup_path.unlink()
                    if metadata_path.exists():
                        metadata_path.unlink()

                    removed.append(backup_name)
                except Exception as e:
                    errors.append(f"{backup.get('backup_name', 'unknown')}: {e}")

        return {
            'success': len(errors) == 0,
            'dry_run': dry_run,
            'would_remove' if dry_run else 'removed': [b['backup_name'] for b in to_remove],
            'keeping': [b['backup_name'] for b in to_keep],
            'errors': errors if errors else None
        }


def get_backup_manager_for_project() -> Optional[BackupManager]:
    """
    Get BackupManager for the current project (detects .skein directory).

    Returns:
        BackupManager instance or None if not in a project
    """
    from pathlib import Path

    # Find project root (directory containing .skein)
    current = Path.cwd()
    while current != current.parent:
        skein_dir = current / '.skein'
        if skein_dir.exists() and skein_dir.is_dir():
            data_dir = skein_dir / 'data'
            return BackupManager(data_dir)
        current = current.parent

    return None
