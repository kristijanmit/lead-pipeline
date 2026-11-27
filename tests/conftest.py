import pytest

from pipeline.enrichers.contact import ContactEnricher


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_fetch_homepage: keep ContactEnricher._fetch_homepage (the test fakes requests.get itself)"
    )


@pytest.fixture(autouse=True)
def no_live_homepage_fetch(request, monkeypatch):
    """Discovery guesses domains for no-website leads; the suite never touches
    the network, so every guessed homepage is unreachable. Tests that exercise
    discovery stub _fetch_homepage on their own enricher instance; tests marked
    real_fetch_homepage fake requests.get instead."""
    if request.node.get_closest_marker("real_fetch_homepage"):
        return

    def unreachable(self, domain):
        raise RuntimeError("homepage unreachable (network disabled in tests)")

    monkeypatch.setattr(ContactEnricher, "_fetch_homepage", unreachable)
