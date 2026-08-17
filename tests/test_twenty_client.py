"""Tests for the Twenty client, driven by a fake transport.

No live workspace is needed: httpx's MockTransport answers the requests, so
these assert the contract this plugin depends on — bearer auth, the REST and
metadata paths, the record limit, and tolerance of the response shapes Twenty
returns across versions.
"""

from __future__ import annotations

import httpx
import pytest

from bloy_dev_agent.features.twenty.client import (
    MAX_RECORDS_PER_REQUEST,
    TwentyClient,
    TwentyError,
    TwentyRateLimited,
)

BASE_URL = "https://workspace.example.test"
API_KEY = "test-key"


def make_client(handler, **kwargs) -> TwentyClient:
    return TwentyClient(
        base_url=BASE_URL,
        api_key=API_KEY,
        transport=httpx.MockTransport(handler),
        # Keep tests fast: the limiter sleeps between calls at low rates.
        rate_limit_per_minute=kwargs.pop("rate_limit_per_minute", 6000),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Auth and configuration
# ---------------------------------------------------------------------------


def test_requires_base_url_and_key():
    with pytest.raises(TwentyError):
        TwentyClient(base_url="", api_key=API_KEY)
    with pytest.raises(TwentyError):
        TwentyClient(base_url=BASE_URL, api_key="")


def test_sends_bearer_token_and_hits_the_rest_path():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"data": {"issues": []}})

    make_client(handler).list_records("issues")

    assert seen["auth"] == f"Bearer {API_KEY}"
    assert seen["url"].startswith(f"{BASE_URL}/rest/issues")


def test_trailing_slash_in_base_url_does_not_double_up():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": {"issues": []}})

    TwentyClient(
        base_url=f"{BASE_URL}/",
        api_key=API_KEY,
        transport=httpx.MockTransport(handler),
        rate_limit_per_minute=6000,
    ).list_records("issues")

    assert "//rest" not in seen["url"].replace("https://", "")


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def test_limit_is_capped_at_the_documented_maximum():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["limit"] = request.url.params.get("limit")
        return httpx.Response(200, json={"data": {"issues": []}})

    make_client(handler).list_records("issues", limit=5_000)

    assert seen["limit"] == str(MAX_RECORDS_PER_REQUEST)


def test_filter_and_order_are_passed_through_untouched():
    seen: dict = {}
    expression = 'updatedAt[gte]:"2026-08-01T00:00:00Z"'

    def handler(request: httpx.Request) -> httpx.Response:
        seen["filter"] = request.url.params.get("filter")
        seen["orderBy"] = request.url.params.get("orderBy")
        return httpx.Response(200, json={"data": {"issues": []}})

    make_client(handler).list_records(
        "issues", filter_expression=expression, order_by="updatedAt"
    )

    assert seen["filter"] == expression
    assert seen["orderBy"] == "updatedAt"


@pytest.mark.parametrize(
    "payload",
    [
        {"data": {"issues": [{"id": "1"}, {"id": "2"}]}},
        {"issues": [{"id": "1"}, {"id": "2"}]},
        [{"id": "1"}, {"id": "2"}],
    ],
    ids=["data-wrapped", "bare-key", "bare-list"],
)
def test_list_tolerates_the_response_shapes_twenty_uses(payload):
    client = make_client(lambda request: httpx.Response(200, json=payload))
    assert [r["id"] for r in client.list_records("issues")] == ["1", "2"]


def test_get_record_unwraps_a_singular_payload():
    client = make_client(
        lambda request: httpx.Response(200, json={"data": {"issue": {"id": "42"}}})
    )
    assert client.get_record("issues", "42")["id"] == "42"


def test_update_sends_only_the_changed_fields():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"data": {"issue": {"id": "42"}}})

    make_client(handler).update_record("issues", "42", {"status": "In Progress"})

    assert seen["method"] == "PATCH"
    assert "In Progress" in seen["body"]
    assert "title" not in seen["body"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "fragment"),
    [(401, "API key"), (403, "permission"), (404, "not found")],
)
def test_http_errors_become_readable_messages(status, fragment):
    client = make_client(lambda request: httpx.Response(status, json={}))
    with pytest.raises(TwentyError) as excinfo:
        client.list_records("issues")
    assert excinfo.value.status_code == status
    assert fragment.lower() in excinfo.value.message.lower()


def test_rate_limit_raises_its_own_type_so_callers_can_back_off():
    client = make_client(lambda request: httpx.Response(429, json={}))
    with pytest.raises(TwentyRateLimited):
        client.list_records("issues")


def test_unreachable_host_is_reported_not_swallowed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(TwentyError) as excinfo:
        make_client(handler).list_records("issues")
    assert "could not reach" in excinfo.value.message.lower()


# ---------------------------------------------------------------------------
# Metadata discovery
# ---------------------------------------------------------------------------


METADATA_PAYLOAD = {
    "data": {
        "objects": {
            "edges": [
                {
                    "node": {
                        "nameSingular": "issue",
                        "namePlural": "issues",
                        "labelSingular": "Issue",
                        "isSystem": False,
                        "isActive": True,
                        "fields": {
                            "edges": [
                                {"node": {"name": "key", "type": "TEXT", "isActive": True}},
                                {"node": {"name": "status", "type": "SELECT", "isActive": True}},
                                {"node": {"name": "old", "type": "TEXT", "isActive": False}},
                            ]
                        },
                    }
                },
                {
                    "node": {
                        "nameSingular": "archivedThing",
                        "namePlural": "archivedThings",
                        "labelSingular": "Archived",
                        "isSystem": False,
                        "isActive": False,
                        "fields": {"edges": []},
                    }
                },
            ]
        }
    }
}


def test_describe_objects_reads_custom_objects_and_active_fields():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json=METADATA_PAYLOAD)

    objects = make_client(handler).describe_objects()

    assert seen["path"] == "/metadata"
    assert [o.name_plural for o in objects] == ["issues"]
    issue = objects[0]
    assert issue.is_custom is True
    assert issue.field_names() == ["key", "status"]  # inactive field dropped


def test_describe_objects_surfaces_graphql_errors():
    client = make_client(
        lambda request: httpx.Response(
            200, json={"errors": [{"message": "permission denied"}]}
        )
    )
    with pytest.raises(TwentyError) as excinfo:
        client.describe_objects()
    assert "permission denied" in excinfo.value.message


def test_record_url_uses_the_show_page_path():
    client = make_client(lambda request: httpx.Response(200, json={}))
    assert (
        client.record_url("issue", "abc-123")
        == f"{BASE_URL}/object/issue/abc-123"
    )


# ---------------------------------------------------------------------------
# The key humans use wins over the key Twenty mints
# ---------------------------------------------------------------------------


def test_a_ticket_key_in_the_title_becomes_the_branch_key():
    """Twenty numbers issues itself, but the branch must match the real ticket.

    The board carries upstream tickets whose key lives in the title (BLS-1064),
    while Twenty mints its own sequence (BLOY-4). Naming the branch after
    Twenty's key would make it unfindable by the name anyone actually uses.
    """
    from bloy_dev_agent.features import workspace
    from bloy_dev_agent.features.twenty import mapping

    issue = mapping.normalize_issue(
        {
            "id": "x",
            "issueKey": "BLOY-4",
            "title": "BLS-1064: Chỉ load translation của published language",
        }
    )

    assert issue.key == "BLS-1064"
    assert issue.record_key == "BLOY-4", "Twenty's key stays traceable"
    assert issue.title == "Chỉ load translation của published language"
    assert workspace.branch_name(issue.key) == "bloy/bls-1064"


def test_twenty_own_key_is_used_when_the_title_has_none():
    from bloy_dev_agent.features.twenty import mapping

    issue = mapping.normalize_issue(
        {"id": "x", "issueKey": "BLOY-7", "title": "Sửa lỗi tính điểm"}
    )

    assert issue.key == "BLOY-7"
    assert issue.title == "Sửa lỗi tính điểm"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("BLS-1064: a", "BLS-1064"),
        ("BLS-1064 - a", "BLS-1064"),
        ("bls-1064: a", ""),          # lowercase is prose, not a key
        ("Fix BLS-1064 later", ""),   # must be a prefix, not a mention
        ("A-1: a", ""),               # one-letter project keys are too loose
        ("", ""),
    ],
)
def test_only_a_real_key_prefix_is_taken(title, expected):
    from bloy_dev_agent.features.twenty import mapping

    assert mapping.split_title_key(title)[0] == expected


def test_the_prompt_is_not_indented_like_a_code_block():
    """dedent must run before interpolation, not after.

    The ticket body contains lines at column zero, so the common prefix across
    the interpolated string is empty and ``dedent`` strips nothing — leaving
    every framing line indented eight spaces, which reads as a code block
    rather than instructions. Observed on a live run.
    """
    from bloy_dev_agent.features.twenty import mapping

    issue = mapping.normalize_issue(
        {
            "id": "x",
            "issueKey": "BLOY-6",
            "title": "BLS-1066: Exclude Gift Card",
            "description": "Dòng ở cột 0\nDòng thứ hai cũng vậy",
        }
    )

    prompt = mapping.build_prompt(issue, "/worktrees/x", implement=True, monorepo="/monorepo")

    framing = [
        line
        for line in prompt.splitlines()
        if line.startswith("You are working") or line.startswith("Ticket ")
    ]
    assert framing, "framing lines should be present"
    for line in framing:
        assert not line.startswith(" "), f"indented framing line: {line!r}"


# ---------------------------------------------------------------------------
# Transport retry
# ---------------------------------------------------------------------------


def test_a_transient_timeout_is_retried_then_succeeds(monkeypatch):
    """Twenty answers slowly for a minute after a restart; one blip is not fatal."""
    from bloy_dev_agent.features.twenty import client as mod

    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"data": {"issues": []}})

    c = TwentyClient(
        base_url="http://twenty.test",
        api_key="k",
        transport=httpx.MockTransport(handler),
    )

    assert c.list_records("issues") == []
    assert calls["n"] == 2, "should have retried exactly once"


def test_a_persistent_outage_still_raises(monkeypatch):
    from bloy_dev_agent.features.twenty import client as mod

    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("refused", request=request)

    c = TwentyClient(
        base_url="http://twenty.test",
        api_key="k",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(TwentyError, match="Could not reach Twenty"):
        c.list_records("issues")
    assert calls["n"] == mod.TRANSPORT_ATTEMPTS


def test_an_http_error_is_not_retried(monkeypatch):
    """A 403 will not become a 200; retrying only wastes the rate-limit budget."""
    from bloy_dev_agent.features.twenty import client as mod

    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403)

    c = TwentyClient(
        base_url="http://twenty.test",
        api_key="k",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(TwentyError):
        c.list_records("issues")
    assert calls["n"] == 1
