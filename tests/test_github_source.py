"""GitHub enrichment: org detection, pattern inference, address construction."""

from __future__ import annotations

import pytest

from src.github_source import (
    GitHubClient, apply_pattern, extract_org, local_pattern, name_parts,
)


# ------------------------------------------------------------- org detection

def test_picks_the_most_linked_org():
    html = """
    <a href="https://github.com/ubicloud">gh</a>
    <a href="https://github.com/ubicloud/ubicloud">repo</a>
    <a href="https://github.com/features/actions">unrelated</a>
    """
    assert extract_org(html) == "ubicloud"


def test_ignores_github_site_paths():
    html = '<a href="https://github.com/pricing">p</a><a href="https://github.com/login">l</a>'
    assert extract_org(html) == ""


def test_no_github_link_yields_nothing():
    assert extract_org("<p>no links here</p>") == ""
    assert extract_org("") == ""


# --------------------------------------------------------- pattern inference

@pytest.mark.parametrize("email,name,expected", [
    ("enes@ubicloud.com", "Enes Cakir", "first"),
    ("ada.lovelace@acme.ai", "Ada Lovelace", "first.last"),
    ("adalovelace@acme.ai", "Ada Lovelace", "firstlast"),
    ("adal@acme.ai", "Ada Lovelace", "firstl"),
    ("alovelace@acme.ai", "Ada Lovelace", "flast"),
    ("ada_lovelace@acme.ai", "Ada Lovelace", "first_last"),
    ("lovelace@acme.ai", "Ada Lovelace", "last"),
    ("random123@acme.ai", "Ada Lovelace", ""),
    ("ada@acme.ai", "", ""),
])
def test_local_pattern_detection(email, name, expected):
    assert local_pattern(email, name) == expected


def test_pattern_handles_middle_names():
    assert local_pattern("som.mohapatra@acme.ai", "Som Ranjan Mohapatra") == "first.last"


# ------------------------------------------------------ address construction

@pytest.mark.parametrize("pattern,expected", [
    ("first", "ada@acme.ai"),
    ("first.last", "ada.lovelace@acme.ai"),
    ("firstlast", "adalovelace@acme.ai"),
    ("firstl", "adal@acme.ai"),
    ("flast", "alovelace@acme.ai"),
    ("first_last", "ada_lovelace@acme.ai"),
    ("last", "lovelace@acme.ai"),
])
def test_apply_pattern(pattern, expected):
    assert apply_pattern(pattern, "Ada", "Lovelace", "acme.ai") == expected


def test_apply_pattern_needs_a_last_name_where_relevant():
    assert apply_pattern("first.last", "Cher", "", "acme.ai") == ""
    assert apply_pattern("first", "Cher", "", "acme.ai") == "cher@acme.ai"


def test_apply_pattern_rejects_unknown_pattern():
    assert apply_pattern("", "Ada", "Lovelace", "acme.ai") == ""
    assert apply_pattern("weird", "Ada", "Lovelace", "acme.ai") == ""


# ------------------------------------------------------------- harvesting

class FakeClient(GitHubClient):
    """GitHubClient with the HTTP layer replaced by a scripted route table."""

    def __init__(self, routes):
        super().__init__(max_calls=50)
        self.routes = routes
        self.requested = []

    def _get(self, path):
        self.requested.append(path)
        for key, value in self.routes.items():
            if path.startswith(key):
                return value
        return None


def commit(email, name):
    return {"commit": {"author": {"email": email, "name": name}}}


def test_harvest_collects_on_domain_emails_and_pattern():
    client = FakeClient({
        "/orgs/ubicloud/repos": [{"name": "ubicloud"}],
        "/repos/ubicloud/ubicloud/commits": [
            commit("enes@ubicloud.com", "Enes Cakir"),
            commit("furkan@ubicloud.com", "Furkan Sahin"),
            commit("outsider@gmail.com", "Someone Else"),
            commit("bot@users.noreply.github.com", "bot"),
        ],
    })
    found = client.harvest("ubicloud", "ubicloud.com")
    assert set(found.emails) == {"enes@ubicloud.com", "furkan@ubicloud.com"}
    assert found.pattern == "first"


def test_harvest_falls_back_to_user_repos():
    client = FakeClient({
        "/users/solodev/repos": [{"name": "thing"}],
        "/repos/solodev/thing/commits": [commit("ada@solo.dev", "Ada Lovelace")],
    })
    found = client.harvest("solodev", "solo.dev")
    assert found.emails == {"ada@solo.dev": "Ada Lovelace"}


def test_harvest_survives_an_org_with_no_repos():
    found = FakeClient({}).harvest("ghost", "ghost.com")
    assert found.emails == {} and found.pattern == ""


def test_noreply_addresses_are_dropped():
    client = FakeClient({
        "/orgs/x/repos": [{"name": "r"}],
        "/repos/x/r/commits": [commit("1234+bob@users.noreply.github.com", "Bob")],
    })
    assert FakeClient.harvest(client, "x", "users.noreply.github.com").emails == {}


def test_budget_stops_further_calls():
    client = FakeClient({"/orgs/x/repos": [{"name": "r"}]})
    client.budget = type(client.budget)(0, "github")
    assert client.harvest("x", "x.com").emails == {}


def test_rate_limited_client_stops_asking():
    client = FakeClient({})
    client.exhausted = True
    client._get = GitHubClient._get.__get__(client)
    assert client._get("/anything") is None


# ------------------------------------- login-style author names (real world)

@pytest.mark.parametrize("email,login,label,expected", [
    ("serafin@inkeep.com", "serafin-garcia", "inkeep", "first"),
    ("shagun.singh@inkeep.com", "shagun-singh-inkeep", "inkeep", "first.last"),
    ("omar@inkeep.com", "omar-inkeep", "inkeep", "first"),
    ("jane.doe@acme.ai", "jane.doe", "acme", "first.last"),
])
def test_pattern_from_github_login_names(email, login, label, expected):
    """Commit authors are often logins, not real names."""
    assert local_pattern(email, login, label) == expected


def test_company_token_is_stripped_only_when_it_is_extra():
    assert name_parts("shagun-singh-inkeep", "inkeep") == ["shagun", "singh"]
    # If the whole name is the company, keep it rather than returning nothing.
    assert name_parts("inkeep", "inkeep") == ["inkeep"]


def test_harvest_infers_pattern_from_logins():
    client = FakeClient({
        "/orgs/inkeep/repos": [{"name": "core"}],
        "/repos/inkeep/core/commits": [
            commit("serafin@inkeep.com", "serafin-garcia"),
            commit("omar@inkeep.com", "omar-inkeep"),
        ],
    })
    found = client.harvest("inkeep", "inkeep.com")
    assert found.pattern == "first"
