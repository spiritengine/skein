"""Tests for SKEIN analytics functions."""

import pytest
import sys
from pathlib import Path

# Add parent directory to path to import analytics module
sys.path.insert(0, str(Path(__file__).parent.parent))

from client.analytics import (
    analyze_folios_by_type,
    analyze_folios_by_status,
    analyze_folios_by_site,
    get_folio_stats,
)


class TestAnalyzeFoliosByType:
    """Test suite for analyze_folios_by_type function."""

    def test_empty_list(self):
        """Empty list should return empty dict."""
        result = analyze_folios_by_type([])
        assert result == {}

    def test_single_type(self):
        """Single folio type should return count of 1."""
        folios = [{"type": "brief", "title": "Test"}]
        result = analyze_folios_by_type(folios)
        assert result == {"brief": 1}

    def test_multiple_same_type(self):
        """Multiple folios of same type should sum correctly."""
        folios = [
            {"type": "brief", "title": "Test 1"},
            {"type": "brief", "title": "Test 2"},
            {"type": "brief", "title": "Test 3"},
        ]
        result = analyze_folios_by_type(folios)
        assert result == {"brief": 3}

    def test_multiple_types(self):
        """Multiple folio types should each have correct count."""
        folios = [
            {"type": "brief", "title": "Brief 1"},
            {"type": "issue", "title": "Issue 1"},
            {"type": "friction", "title": "Friction 1"},
            {"type": "brief", "title": "Brief 2"},
            {"type": "issue", "title": "Issue 2"},
        ]
        result = analyze_folios_by_type(folios)
        assert result == {"brief": 2, "issue": 2, "friction": 1}

    def test_missing_type_field(self):
        """Folios without type field should be skipped."""
        folios = [
            {"type": "brief", "title": "Brief 1"},
            {"title": "No type"},
            {"type": "issue", "title": "Issue 1"},
        ]
        result = analyze_folios_by_type(folios)
        assert result == {"brief": 1, "issue": 1}


class TestAnalyzeFoliosByStatus:
    """Test suite for analyze_folios_by_status function."""

    def test_empty_list(self):
        """Empty list should return empty dict."""
        result = analyze_folios_by_status([])
        assert result == {}

    def test_single_status(self):
        """Single folio should return count of 1."""
        folios = [{"type": "brief", "status": "open"}]
        result = analyze_folios_by_status(folios)
        assert result == {"open": 1}

    def test_multiple_statuses(self):
        """Multiple statuses should each have correct count."""
        folios = [
            {"type": "brief", "status": "open"},
            {"type": "issue", "status": "open"},
            {"type": "friction", "status": "closed"},
            {"type": "brief", "status": "open"},
        ]
        result = analyze_folios_by_status(folios)
        assert result == {"open": 3, "closed": 1}

    def test_missing_status_field(self):
        """Folios without status field should use 'unknown'."""
        folios = [
            {"type": "brief", "status": "open"},
            {"type": "issue"},
            {"type": "friction", "status": "closed"},
        ]
        result = analyze_folios_by_status(folios)
        assert result == {"open": 1, "closed": 1, "unknown": 1}


class TestAnalyzeFoliosBySite:
    """Test suite for analyze_folios_by_site function."""

    def test_empty_list(self):
        """Empty list should return empty dict."""
        result = analyze_folios_by_site([])
        assert result == {}

    def test_single_site(self):
        """Single folio should return count of 1 for its site."""
        folios = [{"type": "brief", "site_id": "site-1"}]
        result = analyze_folios_by_site(folios)
        assert result == {"site-1": 1}

    def test_multiple_sites(self):
        """Multiple sites should each have correct count."""
        folios = [
            {"type": "brief", "site_id": "site-1"},
            {"type": "issue", "site_id": "site-1"},
            {"type": "friction", "site_id": "site-2"},
            {"type": "brief", "site_id": "site-3"},
        ]
        result = analyze_folios_by_site(folios)
        assert result == {"site-1": 2, "site-2": 1, "site-3": 1}

    def test_missing_site_field(self):
        """Folios without site_id field should use 'unknown'."""
        folios = [
            {"type": "brief", "site_id": "site-1"},
            {"type": "issue"},
            {"type": "friction", "site_id": "site-2"},
        ]
        result = analyze_folios_by_site(folios)
        assert result == {"site-1": 1, "unknown": 1, "site-2": 1}


class TestGetFolioStats:
    """Test suite for get_folio_stats function."""

    def test_empty_list(self):
        """Empty list should return all empty stats."""
        result = get_folio_stats([])
        assert result == {
            "total": 0,
            "by_type": {},
            "by_status": {},
            "by_site": {},
        }

    def test_comprehensive_stats(self):
        """Should return all breakdown stats correctly."""
        folios = [
            {"type": "brief", "status": "open", "site_id": "site-1"},
            {"type": "issue", "status": "open", "site_id": "site-1"},
            {"type": "friction", "status": "closed", "site_id": "site-2"},
            {"type": "brief", "status": "closed", "site_id": "site-2"},
        ]
        result = get_folio_stats(folios)

        assert result["total"] == 4
        assert result["by_type"] == {"brief": 2, "issue": 1, "friction": 1}
        assert result["by_status"] == {"open": 2, "closed": 2}
        assert result["by_site"] == {"site-1": 2, "site-2": 2}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
