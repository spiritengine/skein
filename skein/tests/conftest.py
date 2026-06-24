"""Pytest configuration for the skein package test tree."""


def pytest_addoption(parser):
    # Registered at the test-tree root so the flag parses regardless of which
    # subpath is selected. The @pytest.mark.interactive consumer lives in
    # skein/tests/test_signing/conftest.py, but e.g.
    # `pytest skein/tests/conformance --run-interactive` must still parse it.
    parser.addoption(
        "--run-interactive",
        action="store_true",
        default=False,
        help="Run tests marked @pytest.mark.interactive (require a human at the "
        "terminal to complete an OIDC handshake — never run in CI).",
    )
