"""VC portfolio discovery and founder names read off company sites."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from src.scraper import parse_site_founders
from src.vc_source import (
    Portfolio, SiteRanks, discover, extract_companies, load_portfolios,
    pages_for_today, registrable, site_summary,
)

PAGE = "https://fund.vc/portfolio"


def test_registrable_collapses_subdomains():
    assert registrable("www.acme.ai") == "acme.ai"
    assert registrable("jobs.acme.ai") == "acme.ai"
    assert registrable("app.acme.co.uk") == "acme.co.uk"


def test_anchor_links_give_domain_and_label():
    html = """
    <a href="https://www.acme.ai/">Acme</a>
    <a href="https://beta.io"><img alt="Beta logo" src="x.png"></a>
    <a href="https://gamma.dev">Visit website</a>
    <a href="/team">Our team</a>
    <a href="https://linkedin.com/company/fund">LinkedIn</a>
    """
    found = extract_companies(html, PAGE)
    assert found == {"acme.ai": "Acme", "beta.io": "Beta", "gamma.dev": ""}


def test_call_to_action_and_url_labels_are_dropped():
    html = """
    <a href="https://acme.ai">Visit the Website</a>
    <a href="https://beta.io">www.beta.io/ (opens in new tab)</a>
    """
    assert extract_companies(html, PAGE) == {"acme.ai": "", "beta.io": ""}


def test_sister_funds_and_companies_abroad_are_skipped():
    html = """
    <a href="https://fundcrypto.com">Fund Crypto</a>
    <a href="https://seguros.com.br">Seguros</a>
    <a href="https://acme.de">Acme DE</a>
    <a href="https://maple.ca">Maple</a>
    """
    assert extract_companies(html, "https://fund.com/portfolio") == {"maple.ca": "Maple"}


def test_links_inside_a_json_payload_are_read_and_infra_dropped():
    html = ('<script>{"companies":[{"url":"https:\\/\\/www.delta.ai"},'
            '{"img":"https://cdn.sanity.io/x.png"},{"u":"https://fund.vc/a"}]}'
            '</script><script src="https://www.googletagmanager.com/gtm.js">'
            '</script>')
    assert extract_companies(html, PAGE) == {"delta.ai": ""}


def test_site_summary_prefers_og_tags():
    html = ('<head><title>Acme | Invoices on autopilot</title>'
            '<meta property="og:description" content="AI for  invoices."></head>')
    assert site_summary(html) == ("Acme", "AI for invoices.")


def test_site_summary_picks_the_title_segment_matching_the_domain():
    html = "<title>Generative AI for Clinical Conversations | Abridge</title>"
    assert site_summary(html, "abridge.com")[0] == "Abridge"
    assert site_summary("<title>Tagline only here</title>", "acme.ai")[0] == ""


def test_discover_skips_a_site_that_redirects_elsewhere(tmp_path):
    vc = tmp_path / "vc.txt"
    vc.write_text(f"Fund | US | {PAGE}\n", encoding="utf-8")
    fetcher = _Fetcher({PAGE: '<a href="https://old.ai">Old</a>',
                        "https://old.ai": "<title>Buyer</title>"})
    fetcher.last_url = "https://www.buyer.com/"
    found, stats = discover(fetcher, SimpleNamespace(vc_portfolios_path=vc),
                            _Db(), _ranks(tmp_path))
    assert found == [] and stats.moved == 1


def test_portfolio_file_skips_comments_and_bad_lines(tmp_path):
    path = tmp_path / "vc.txt"
    path.write_text("# header\nFund A | US | https://a.vc/p\nbroken line\n"
                    "Fund B | CA | https://b.vc/p  # trailing\n", encoding="utf-8")
    assert load_portfolios(path) == [Portfolio("Fund A", "US", "https://a.vc/p"),
                                     Portfolio("Fund B", "CA", "https://b.vc/p")]


def test_rotation_covers_every_fund():
    funds = [Portfolio(str(i), "US", f"https://{i}.vc") for i in range(5)]
    seen = set()
    for day in range(1, 6):
        seen |= {p.fund for p in pages_for_today(funds, 2, date(2026, 11, day))}
    assert seen == {f.fund for f in funds}


class _Fetcher:
    def __init__(self, pages):
        self.pages = pages
        self.budget = SimpleNamespace(exhausted=False)

    def get(self, url, respect_robots=True):
        return self.pages.get(url)


class _Db:
    def __init__(self, known=()):
        self.known = set(known)

    def company_exists(self, domain):
        return domain in self.known


def _ranks(tmp_path, big=()):
    cache = tmp_path / "tranco.txt"
    cache.write_text("\n".join(big or ["example.com"]), encoding="utf-8")
    return SiteRanks(cache, 20_000)


def test_discover_skips_known_big_and_dead_companies(tmp_path):
    vc = tmp_path / "vc.txt"
    vc.write_text(f"Fund | US | {PAGE}\n", encoding="utf-8")
    fetcher = _Fetcher({
        PAGE: ('<a href="https://known.ai">Known</a><a href="https://airbnb.com">'
               'Airbnb</a><a href="https://down.ai">Down</a>'
               '<a href="https://fresh.ai">Fresh</a>'),
        "https://fresh.ai": '<meta name="description" content="Fresh things.">',
    })
    found, stats = discover(fetcher, SimpleNamespace(vc_portfolios_path=vc),
                            _Db(known={"known.ai"}),
                            _ranks(tmp_path, big=["airbnb.com"]), limit=10)
    assert [(c.name, c.domain, c.source, c.one_liner) for c in found] == [
        ("Fresh", "fresh.ai", "vc", "Fresh things.")]
    assert (stats.already_known, stats.too_big, stats.dead) == (1, 1, 1)


def test_discover_adds_nothing_without_a_ranking(tmp_path, monkeypatch):
    vc = tmp_path / "vc.txt"
    vc.write_text(f"Fund | US | {PAGE}\n", encoding="utf-8")
    ranks = SiteRanks(tmp_path / "missing.txt", 20_000)
    monkeypatch.setattr(ranks, "_refresh", lambda session: None)
    fetcher = _Fetcher({PAGE: '<a href="https://fresh.ai">Fresh</a>'})
    found, _ = discover(fetcher, SimpleNamespace(vc_portfolios_path=vc), _Db(),
                        ranks)
    assert found == []


# ---------------------------------------------------------- site founders

def test_team_page_name_followed_by_founder_title():
    html = """<div><h2>Meet the team</h2>
      <div><h3>Priya Raman</h3><p>Co-founder &amp; CEO</p></div>
      <div><h3>Tom Okafor</h3><p>Head of Growth</p></div>
      <div><h3>Lena Fischer</h3><p>CTO</p></div></div>"""
    people = parse_site_founders(html)
    assert [(p.first_name, p.last_name, p.role) for p in people] == [
        ("Priya", "Raman", "Co-founder & CEO"), ("Lena", "Fischer", "CTO")]


def test_inline_name_and_title():
    people = parse_site_founders("<p>Sam Lee, Founder and CEO</p>")
    assert [p.full_name for p in people] == ["Sam Lee"]


def test_json_ld_founder():
    html = ('<script type="application/ld+json">{"@type":"Organization",'
            '"founder":[{"@type":"Person","name":"Ada Byron"}]}</script>')
    assert [p.full_name for p in parse_site_founders(html)] == ["Ada Byron"]


def test_headings_are_not_taken_for_names():
    html = "<h2>Our Founders</h2><p>Founder stories</p><h2>About Us</h2><p>CEO letter</p>"
    assert parse_site_founders(html) == []
