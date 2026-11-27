"""Shared audit core (pipeline/audit.py): signals from fixture HTML, probe
failure kinds, and the probe/Lighthouse split — no live network calls."""

from pathlib import Path

import pytest
import requests

from pipeline import audit
from pipeline.audit import audit_site, classify_probe_error, extract_signals
from pipeline.enrichers.audit import AuditEnricher
from pipeline.enrichers.base import apply_enrichers
from pipeline.schema import Lead

FIXTURES = Path(__file__).parent / "fixtures"
YEAR = 2026


def _signals(name: str) -> dict:
    return extract_signals((FIXTURES / name).read_text(encoding="utf-8"), YEAR)


def test_signals_old_serbian_site():
    s = _signals("old_site.html")
    assert s["copyright_year"] == 2017  # "Sva prava zadržana", no © — the 2024 news line is not a copyright
    assert s["has_viewport_meta"] is False
    assert s["has_booking"] is False
    assert s["has_click_to_call"] is False
    assert s["has_messaging_link"] is False
    assert s["ads_tags"] == []


def test_signals_modern_site_with_booking_widget():
    s = _signals("modern_site.html")
    assert s["copyright_year"] == 2025  # "© 2019–2025 … All rights reserved": the highest year
    assert s["has_viewport_meta"] is True
    assert s["has_booking"] is True  # Calendly
    assert s["has_click_to_call"] is True
    assert s["has_messaging_link"] is True  # wa.me
    assert s["ads_tags"] == []


def test_signals_ads_site():
    s = _signals("ads_site.html")
    assert s["ads_tags"] == ["meta_pixel", "gtm"]  # GA4 alone is not an ads tag
    assert s["has_booking"] is True  # a date input
    assert s["has_messaging_link"] is True  # viber://
    assert s["copyright_year"] == 2022  # 2031 is after the current year; "sva prava zadrzana" without ž


def test_signals_existing_contact_fixture():
    s = _signals("contact_page.html")
    assert s["copyright_year"] == 2026
    assert s["has_click_to_call"] is True
    assert s["has_viewport_meta"] is False
    assert s["ads_tags"] == [] and s["has_booking"] is False


@pytest.mark.parametrize("html, year", [
    ("<footer>Sva prava zadrzana © 2015</footer>", 2015),
    ("<footer>All rights reserved 2020</footer>", 2020),
    ("<footer>Copyright 2018-2023</footer>", 2023),
    ("<p>Osnovano 2010.</p>", None),  # a year with no copyright marker
    ("<footer>© Firma</footer>", None),
])
def test_copyright_year_markers(html, year):
    assert extract_signals(html, YEAR)["copyright_year"] == year


def _conn_error(message):
    return requests.exceptions.ConnectionError(message)


@pytest.mark.parametrize("error, kind", [
    (_conn_error("HTTPSConnectionPool: NameResolutionError: Failed to resolve 'x.rs'"), "dns"),
    (_conn_error("[Errno 8] nodename nor servname provided, or not known"), "dns"),
    (_conn_error("[Errno 61] Connection refused"), "refused"),
    (requests.exceptions.ConnectTimeout("connect timeout"), "timeout"),
    (requests.exceptions.ReadTimeout("read timeout"), "timeout"),
    (requests.exceptions.SSLError("certificate verify failed"), "ssl"),
    (requests.exceptions.HTTPError("503 Server Error"), "http_error"),
    (_conn_error("Connection aborted, RemoteDisconnected"), "other"),
    (requests.exceptions.TooManyRedirects("loop"), "other"),
])
def test_classify_probe_error(error, kind):
    assert classify_probe_error(error) == kind


def _fake_get(errors_by_scheme):
    def get(url, **kwargs):
        raise errors_by_scheme[url.split(":")[0]]
    return get


def test_probe_keeps_the_most_alive_failure(monkeypatch):
    # bad cert on https + refused on http: something is there — not a dead domain
    monkeypatch.setattr(audit.requests, "get", _fake_get({
        "https": requests.exceptions.SSLError("bad cert"),
        "http": _conn_error("Connection refused"),
    }))
    with pytest.raises(audit.ProbeError) as e:
        audit.probe("x.rs", 1)
    assert e.value.kind == "ssl"


def _config():
    return {"enrich": {"http_timeout_s": 1, "lighthouse": {"binary": "lighthouse", "timeout_s": 1}}}


def _lead(**kw):
    return Lead(company="Klinika", domain="klinika.rs", source="maps", **kw)


@pytest.mark.parametrize("error, kind", [
    (_conn_error("Failed to resolve 'klinika.rs'"), "dns"),
    (_conn_error("Connection refused"), "refused"),
    (requests.exceptions.ReadTimeout("read timed out"), "timeout"),
])
def test_dead_or_slow_site_is_data_not_an_exception(monkeypatch, error, kind):
    monkeypatch.setattr(audit.requests, "get", _fake_get({"https": error, "http": error}))
    monkeypatch.setattr(audit, "run_lighthouse", lambda *a: pytest.fail("no Lighthouse for a dead site"))
    out = apply_enrichers(_lead(), [("audit", AuditEnricher(_config()))], retry_delay_s=0)
    assert out.status == "enriched"  # not enrich_failed: re-runs won't retry it forever
    assert out.site_reachable is False and out.probe_error == kind
    assert out.errors and out.errors[0].startswith(f"audit: site unreachable ({kind})")
    assert out.copyright_year is None and out.https is None


class _Response:
    url = "https://klinika.rs/"
    text = (FIXTURES / "modern_site.html").read_text(encoding="utf-8")
    headers = {"Content-Type": "text/html; charset=utf-8"}

    def raise_for_status(self):
        pass


def test_lighthouse_failure_keeps_probe_fields_and_runs_once(monkeypatch):
    calls = []

    def failing_lighthouse(*args):
        calls.append(args)
        raise RuntimeError("lighthouse timed out after 1s on https://klinika.rs/")

    monkeypatch.setattr(audit.requests, "get", lambda url, **k: _Response())
    monkeypatch.setattr(audit, "run_lighthouse", failing_lighthouse)
    out = apply_enrichers(_lead(), [("audit", AuditEnricher(_config()))], retry_delay_s=0)
    assert len(calls) == 1
    assert out.status == "enriched"
    assert out.site_reachable is True and out.probe_error is None
    assert out.https is True and out.has_booking is True and out.copyright_year == 2025
    assert out.lighthouse is None
    assert out.errors == ["audit: lighthouse timed out after 1s on https://klinika.rs/"]


def test_audit_site_success(monkeypatch):
    monkeypatch.setattr(audit.requests, "get", lambda url, **k: _Response())
    monkeypatch.setattr(audit, "run_lighthouse", lambda *a: {
        "categories": {"performance": {"score": 0.4}, "seo": {"score": 0.9}},
        "audits": {"viewport": {"score": 1}},
    })
    result = audit_site(
        "klinika.rs", http_timeout_s=1, lighthouse_binary="lighthouse", lighthouse_timeout_s=1, current_year=YEAR
    )
    assert result.site_reachable and result.lighthouse == {"performance": 40, "seo": 90}
    assert result.mobile_friendly is True and result.lighthouse_error == ""


def test_v9_and_v10_rows_load_with_empty_signals():
    for version in (9, 10):
        lead = Lead.from_dict({"company": "A", "domain": "a.rs", "source": "osm", "schema_version": version})
        assert lead.site_reachable is None and lead.ads_tags is None and lead.probe_error is None


def test_html_text_honours_meta_charset_when_the_header_has_none():
    from types import SimpleNamespace

    from pipeline.audit import html_text

    body = '<html><head><meta charset="utf-8"><title>Dr Isailović u Šapcu</title></head></html>'.encode()
    no_charset = SimpleNamespace(headers={"Content-Type": "text/html"}, content=body,
                                 text=body.decode("iso-8859-1"))
    assert "Isailović u Šapcu" in html_text(no_charset)
    with_charset = SimpleNamespace(headers={"Content-Type": "text/html; charset=utf-8"}, content=body,
                                   text=body.decode("utf-8"))
    assert html_text(with_charset) == with_charset.text
