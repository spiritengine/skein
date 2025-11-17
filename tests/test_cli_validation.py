"""Tests for SKEIN CLI argument validation."""

import pytest
import sys
from pathlib import Path

# Add parent directory to path to import CLI module
sys.path.insert(0, str(Path(__file__).parent.parent))

from client.cli import validate_positional_args
import click


class TestValidatePositionalArgs:
    """Test suite for validate_positional_args function."""

    def test_valid_arguments_pass(self):
        """Valid arguments should not raise any errors."""
        # These should all pass without raising exceptions
        validate_positional_args("site-id", "description", command_name="issue")
        validate_positional_args("my-site", "Some content here", command_name="brief create")
        validate_positional_args("test-site", "Finding details", command_name="finding")

    def test_content_equals_syntax_fails(self):
        """Arguments with content= syntax should raise helpful error."""
        with pytest.raises(click.ClickException) as exc_info:
            validate_positional_args("site-id", "content=test", command_name="issue")
        
        error_msg = str(exc_info.value)
        assert "Incorrect syntax" in error_msg
        assert "content=" in error_msg
        assert "positional arguments" in error_msg
        assert "skein issue" in error_msg

    def test_description_equals_syntax_fails(self):
        """Arguments with description= syntax should raise helpful error."""
        with pytest.raises(click.ClickException) as exc_info:
            validate_positional_args("site-id", "description=test", command_name="finding")
        
        error_msg = str(exc_info.value)
        assert "Incorrect syntax" in error_msg
        assert "description=" in error_msg
        assert "skein finding" in error_msg

    def test_title_equals_syntax_fails(self):
        """Arguments with title= syntax should raise helpful error."""
        with pytest.raises(click.ClickException) as exc_info:
            validate_positional_args("site-id", "title=test", command_name="issue")
        
        error_msg = str(exc_info.value)
        assert "title=" in error_msg

    def test_equals_in_actual_content_passes(self):
        """Equals sign in actual content (not at start) should pass."""
        # These should pass - the equals is part of the actual content
        validate_positional_args(
            "site-id",
            "The equation x=5 is important",
            command_name="issue"
        )
        validate_positional_args(
            "site-id",
            "Config: debug=true and verbose=false",
            command_name="finding"
        )

    def test_hyphenated_arguments_pass(self):
        """Arguments starting with hyphens (flags) should be ignored."""
        # Arguments starting with - are flags, not positional args
        validate_positional_args("site-id", "--content", command_name="issue")

    def test_non_identifier_before_equals_passes(self):
        """Equals with non-identifier before it should pass."""
        validate_positional_args(
            "site-id",
            "value-with-dashes=test",
            command_name="issue"
        )
        validate_positional_args(
            "site-id",
            "123=test",
            command_name="issue"
        )

    def test_multiple_args_with_one_bad(self):
        """If any argument is bad, should raise error."""
        with pytest.raises(click.ClickException):
            validate_positional_args(
                "site-id",
                "good-arg",
                "content=bad",
                command_name="issue"
            )

    def test_first_arg_with_equals_fails(self):
        """Even first argument with name=value syntax should fail."""
        with pytest.raises(click.ClickException) as exc_info:
            validate_positional_args("site=test", "description", command_name="issue")
        
        error_msg = str(exc_info.value)
        assert "site=" in error_msg

    def test_none_and_empty_args_pass(self):
        """None and empty string arguments should not cause errors."""
        validate_positional_args("site-id", "", command_name="issue")
        validate_positional_args("site-id", None, command_name="issue")

    def test_error_message_includes_help_hint(self):
        """Error message should suggest using --help."""
        with pytest.raises(click.ClickException) as exc_info:
            validate_positional_args("site-id", "content=test", command_name="brief create")
        
        error_msg = str(exc_info.value)
        assert "--help" in error_msg
        assert "skein brief create --help" in error_msg


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
