import pytest

from pipeline.enrichers.contact import ContactEnricher


@pytest.fixture(autouse=True)
def no_live_homepage_fetch(monkeypatch):
    """Discovery guesses domains for no-website leads; the suite never touches
    the network, so every guessed homepage is unreachable. Tests that exercise
    discovery stub _fetch_homepage on their own enricher instance."""

    def unreachable(self, domain):
        raise RuntimeError("homepage unreachable (network disabled in tests)")

    monkeypatch.setattr(ContactEnricher, "_fetch_homepage", unreachable)
