"""SiteClient (pipeline/audit.py): same-host spacing, one retry after a 429
(Retry-After, max 10 s), and one shared homepage fetch for contact and the audit.
requests.get, sleep and the clock are faked — no network, no real waiting."""

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
import requests

from pipeline import audit
from pipeline.audit import MAX_RETRY_AFTER_S, SiteClient, retry_after_s
from pipeline.enrichers.audit import AuditEnricher
from pipeline.enrichers.base import apply_enrichers
from pipeline.enrichers.contact import ContactEnricher
from pipeline.schema import Lead, write_jsonl

HTML = '<html><head><meta charset="utf-8"><title>Klinika</title></head><body>' \
       '<a href="mailto:info@klinika.rs">mail</a><a href="/kontakt">Kontakt</a></body></html>'


class _Resp:
    def __init__(self, url, status=200, body=HTML, headers=None):
        self.url, self.status_code = url, status
        self.headers = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}
        self.text, self.content = body, body.encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error for url: {self.url}")


class _Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps: list[float] = []

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds

    def __call__(self):
        return self.now


def _client(delay=1.5):
    clock = _Clock()
    return SiteClient(5, delay, sleep=clock.sleep, clock=clock), clock


def _fake_get(monkeypatch, responses):
    """responses: url -> list of _Resp (popped in order; the last one repeats)."""
    calls = []

    def get(url, **kw):
        calls.append(url)
        queue = responses.get(url) or [_Resp(url, 404, "")]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(audit.requests, "get", get)
    return calls


@pytest.mark.parametrize("value, seconds", [
    ("3", 3.0), ("60", MAX_RETRY_AFTER_S), (None, MAX_RETRY_AFTER_S), ("soon", MAX_RETRY_AFTER_S), ("-5", 0.0),
])
def test_retry_after_parsing_is_capped(value, seconds):
    assert retry_after_s(value) == seconds


def test_retry_after_http_date():
    soon = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=4), usegmt=True)
    assert 2 <= retry_after_s(soon) <= 4
    past = format_datetime(datetime.now(timezone.utc) - timedelta(seconds=30), usegmt=True)
    assert retry_after_s(past) == 0.0


def test_429_waits_retry_after_then_retries_once(monkeypatch):
    url = "https://klinika.rs/"
    calls = _fake_get(monkeypatch, {url: [_Resp(url, 429, "", {"Retry-After": "3"}), _Resp(url)]})
    client, clock = _client(delay=0)
    assert client.get(url).status_code == 200
    assert calls == [url, url] and clock.sleeps == [3.0]


def test_second_429_is_returned_for_the_caller_to_record(monkeypatch):
    url = "https://klinika.rs/"
    calls = _fake_get(monkeypatch, {url: [_Resp(url, 429, "", {"Retry-After": "120"})]})
    client, clock = _client(delay=0)
    assert client.get(url).status_code == 429
    assert calls == [url, url] and clock.sleeps == [MAX_RETRY_AFTER_S]  # capped, one retry only


def test_same_host_requests_are_spaced_other_hosts_are_not(monkeypatch):
    _fake_get(monkeypatch, {})
    client, clock = _client(delay=1.5)
    client.get("https://klinika.rs/robots.txt")
    clock.now += 0.5
    client.get("https://www.klinika.rs/kontakt")  # same site, www stripped
    client.get("https://other.rs/")
    assert clock.sleeps == [1.0]


def test_probe_429_twice_is_recorded_as_unreachable(monkeypatch):
    responses = {
        f"{s}://klinika.rs/": [_Resp(f"{s}://klinika.rs/", 429, "", {"Retry-After": "2"})] for s in ("https", "http")
    }
    calls = _fake_get(monkeypatch, responses)
    monkeypatch.setattr(audit, "run_lighthouse", lambda *a: pytest.fail("no Lighthouse for a site that 429s"))
    enricher = AuditEnricher({"enrich": {"http_timeout_s": 1, "same_domain_delay_s": 0,
                                         "lighthouse": {"binary": "lighthouse", "timeout_s": 1}}})
    clock = _Clock()
    enricher.site = SiteClient(1, 0, sleep=clock.sleep, clock=clock)
    out = apply_enrichers(Lead(company="K", domain="klinika.rs", source="maps"), [("audit", enricher)], retry_delay_s=0)
    assert out.site_reachable is False and out.probe_error == "http_error"
    assert "429" in out.errors[0]
    assert len(calls) == 4 and clock.sleeps == [2.0, 2.0]  # each scheme: one wait, one retry


@pytest.mark.real_fetch_homepage
def test_contact_and_audit_share_one_homepage_fetch(monkeypatch):
    home = "https://klinika.rs/"
    calls = _fake_get(monkeypatch, {
        home: [_Resp(home)],
        "https://klinika.rs/kontakt": [_Resp("https://klinika.rs/kontakt", 429, "", {"Retry-After": "1"}),
                                       _Resp("https://klinika.rs/kontakt")],
    })
    monkeypatch.setattr(audit, "run_lighthouse", lambda url, *a: {"categories": {"performance": {"score": 0.9}}})
    config = {"enrich": {"http_timeout_s": 1, "same_domain_delay_s": 1.5,
                         "lighthouse": {"binary": "lighthouse", "timeout_s": 1}}}
    clock = _Clock()
    shared = SiteClient(1, 1.5, sleep=clock.sleep, clock=clock)
    contact, audit_enricher = ContactEnricher(config), AuditEnricher(config)
    contact.site = audit_enricher.site = shared
    out = apply_enrichers(Lead(company="Klinika", domain="klinika.rs", source="maps"),
                          [("contact", contact), ("audit", audit_enricher)], retry_delay_s=0)
    assert calls.count(home) == 1  # contact fetched it, the audit reused it
    assert calls == ["https://klinika.rs/robots.txt", home, "https://klinika.rs/kontakt", "https://klinika.rs/kontakt"]
    assert out.contact_emails == ["info@klinika.rs"] and out.site_reachable is True and out.lighthouse
    # robots -> home -> kontakt each 1.5 s apart, the 429 waits 1 s, then Lighthouse waits for spacing too
    assert clock.sleeps == [1.5, 1.5, 1.0, 0.5, 1.5]


def test_cmd_enrich_shares_one_site_client(tmp_path, monkeypatch):
    from pipeline import runner

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("")
    run_dir = tmp_path / "data" / "runs" / "r"
    run_dir.mkdir(parents=True)
    write_jsonl(run_dir / "collected.jsonl", [Lead(company="A", domain="a.rs", source="osm")])
    (run_dir / "manifest.json").write_text(json.dumps({"run_id": "r"}))
    seen = []

    def capture(lead, enrichers, retry_delay_s):
        seen.append(enrichers)
        return lead

    monkeypatch.setattr(runner, "apply_enrichers", capture)
    runner.main(["enrich", "--run", "r"])
    clients = {id(e.site) for _, e in seen[0]}
    assert len(clients) == 1 and seen[0][0][1].site.same_domain_delay_s == 1.5
