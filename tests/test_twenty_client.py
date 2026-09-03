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


def test_attachment_file_field_id_finds_the_files_field():
    payload = {
        "data": {
            "objects": {
                "edges": [
                    {
                        "node": {
                            "nameSingular": "issue",
                            "fields": {"edges": [{"node": {"id": "wrong", "name": "file"}}]},
                        }
                    },
                    {
                        "node": {
                            "nameSingular": "attachment",
                            "fields": {
                                "edges": [
                                    {"node": {"id": "not-it", "name": "name"}},
                                    {"node": {"id": "field-xyz", "name": "file"}},
                                ]
                            },
                        }
                    },
                ]
            }
        }
    }
    client = make_client(lambda request: httpx.Response(200, json=payload))
    assert client.attachment_file_field_id() == "field-xyz"


def test_attachment_file_field_id_raises_when_not_found():
    client = make_client(
        lambda request: httpx.Response(200, json={"data": {"objects": {"edges": []}}})
    )
    with pytest.raises(TwentyError):
        client.attachment_file_field_id()


def test_upload_file_rewrites_the_servers_own_host_to_the_configured_base(monkeypatch):
    """Live bug: this deployment's server hands back an upload URL naming a
    host:port that 404s from here, while the exact same path+token against
    the client's own configured base_url works. Pin the rewrite so a future
    change can't silently drop it and reintroduce the 404."""
    seen_put_url = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/metadata":
            body = request.content.decode()
            if "CreateFileUpload" in body:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "createFileUpload": {
                                "fileId": "file-1",
                                "uploadUrl": "http://totally-unreachable-host:9999"
                                "/file-upload/file-1?token=abc",
                                "contentType": "application/octet-stream",
                            }
                        }
                    },
                )
            if "CompleteFileUpload" in body:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "completeFileUpload": {
                                "id": "file-1",
                                "path": "files-field/x/file-1.png",
                                "size": 5,
                                "url": f"{BASE_URL}/file/files-field/file-1?token=abc",
                            }
                        }
                    },
                )
        if request.url.path == "/file-upload/file-1":
            seen_put_url["url"] = str(request.url)
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = make_client(handler)
    result = client.upload_file(b"hello", "shot.png", "field-xyz")

    assert seen_put_url["url"].startswith(BASE_URL)
    assert "totally-unreachable-host" not in seen_put_url["url"]
    assert result["id"] == "file-1"


def test_upload_file_surfaces_a_failed_upload_as_a_twenty_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if "CreateFileUpload" in request.content.decode():
            return httpx.Response(
                200,
                json={
                    "data": {
                        "createFileUpload": {
                            "fileId": "file-1",
                            "uploadUrl": f"{BASE_URL}/file-upload/file-1?token=abc",
                            "contentType": "application/octet-stream",
                        }
                    }
                },
            )
        return httpx.Response(500, text="storage is down")

    client = make_client(handler)
    with pytest.raises(TwentyError):
        client.upload_file(b"hello", "shot.png", "field-xyz")


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


# ---------------------------------------------------------------------------
# normalize_comments: real-timestamp ordering for a migrated comment thread
# ---------------------------------------------------------------------------


def _migrated_comment(record_id, author, ts, body, twenty_created_at="2026-08-10T09:00:00.000Z"):
    """A comment shaped exactly like the Jira->Twenty migration writes it —
    real author/time baked into the body, Twenty's own createdAt is just the
    bulk-import moment (confirmed live: every migrated comment lands within
    the same import day, regardless of the real order things were said)."""
    from bloy_dev_agent.features.twenty import mapping

    return {
        "id": record_id,
        "createdAt": twenty_created_at,
        "createdBy": {"name": "bloy_token"},
        "bodyV2": mapping.text_to_blocknote(f"{author} comment on {ts}\n\n{body}"),
    }


def _native_comment(record_id, author, created_at, body):
    from bloy_dev_agent.features.twenty import mapping

    return {
        "id": record_id,
        "createdAt": created_at,
        "createdBy": {"name": author},
        "bodyV2": mapping.text_to_blocknote(body),
    }


def test_migrated_comments_sort_by_the_real_date_in_the_body_not_twentys_own():
    """The regression this guards: Twenty's createdAt is import time for a
    migrated comment, so sorting by it would show a 2025 comment as newer
    than a 2026 one just because it happened to import second."""
    from bloy_dev_agent.features.twenty import mapping

    records = [
        _migrated_comment("c-new", "Minh VH", "2026-06-09 09:07:37", "bản mới"),
        _migrated_comment("c-old", "Hùng TQ", "2025-11-03 13:12:01", "bản cũ hơn nhiều"),
    ]

    comments = mapping.normalize_comments(records)

    assert [c.id for c in comments] == ["c-old", "c-new"]
    assert comments[0].author == "Hùng TQ"
    assert comments[0].body == "bản cũ hơn nhiều"
    assert comments[0].migrated is True


def test_native_comments_use_twentys_own_created_at():
    from bloy_dev_agent.features.twenty import mapping

    records = [_native_comment("c-1", "SAE.C - Hùng TQ", "2026-08-26T10:40:50.584Z", "Đã fix")]

    comments = mapping.normalize_comments(records)

    assert comments[0].author == "SAE.C - Hùng TQ"
    assert comments[0].created_at == "2026-08-26T10:40:50.584Z"
    assert comments[0].migrated is False


def test_migrated_and_native_comments_interleave_correctly():
    """A thread with old migrated history AND a fresh native reply must put
    the native one last if it really is the most recent."""
    from bloy_dev_agent.features.twenty import mapping

    records = [
        _native_comment("c-native", "Hùng TQ", "2026-08-26T10:40:50.584Z", "Đã fix"),
        _migrated_comment("c-mid", "Minh VH", "2026-06-09 09:07:37", "cũ hơn"),
    ]

    comments = mapping.normalize_comments(records)

    assert [c.id for c in comments] == ["c-mid", "c-native"]


def test_comments_block_marks_only_the_last_one_as_newest():
    from bloy_dev_agent.features.twenty import mapping

    issue = mapping.normalize_issue(
        {"id": "x", "issueKey": "BLOY-1", "title": "Sửa lỗi", "description": "mô tả gốc"}
    )
    comments = mapping.normalize_comments(
        [
            _native_comment("c-1", "A", "2026-08-01T00:00:00.000Z", "bình luận đầu"),
            _native_comment("c-2", "B", "2026-08-02T00:00:00.000Z", "bình luận cuối"),
        ]
    )

    prompt = mapping.build_prompt(issue, "/worktrees/x", implement=True, comments=comments)

    assert "bình luận đầu" in prompt
    assert "bình luận cuối" in prompt
    assert prompt.index("bình luận đầu") < prompt.index("bình luận cuối")
    lines = prompt.splitlines()
    newest_line = next(line for line in lines if "MỚI NHẤT" in line)
    assert "B" in newest_line and "A" not in newest_line


def test_build_prompt_without_comments_omits_the_section():
    """No comments fetched (or none on the ticket) must not add a heading —
    same convention as _skills_block: empty input, empty output."""
    from bloy_dev_agent.features.twenty import mapping

    issue = mapping.normalize_issue({"id": "x", "issueKey": "BLOY-1", "title": "Sửa lỗi"})

    prompt = mapping.build_prompt(issue, "/worktrees/x", implement=True, comments=None)

    assert "Bình luận trên ticket" not in prompt


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
# is_snippet_deliverable: agent's own self-declared heading, never ticket text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "output, expected",
    [
        ("DELIVERABLE: SNIPPET", True),
        ("some analysis\n\nDELIVERABLE: SNIPPET", True),
        ("some analysis\n\nDELIVERABLE: SNIPPET\n\nmore text after", True),
        ("## DELIVERABLE: SNIPPET", True),
        ("deliverable: snippet", True),  # case-insensitive
        ("  DELIVERABLE: SNIPPET  ", True),  # trailing/leading whitespace on the line
        ("", False),
        ("   ", False),
        # Buried mid-paragraph, not a real declaration on its own line.
        ("I considered a DELIVERABLE: SNIPPET approach but rejected it.", False),
        # The old ticket-body marker text, now meaningless — must not be
        # mistaken for the new output-side heading if the agent quotes it back.
        ("The ticket said deliverable: snippet, but I implemented it for real.", False),
        # Discussing the heading without declaring it.
        ('If needed I would end with "DELIVERABLE: SNIPPET" but that is not the case here.', False),
    ],
)
def test_is_snippet_deliverable_requires_a_real_standalone_heading(output, expected):
    from bloy_dev_agent.features.twenty import mapping

    assert mapping.is_snippet_deliverable(output) is expected


@pytest.mark.parametrize(
    "output, expected",
    [
        ("DELIVERABLE: NO CHANGE NEEDED", True),
        ("some analysis\n\nDELIVERABLE: NO CHANGE NEEDED", True),
        ("## DELIVERABLE: NO CHANGE NEEDED", True),
        ("deliverable: no change needed", True),  # case-insensitive
        ("DELIVERABLE:  NO  CHANGE  NEEDED", True),  # extra internal whitespace
        ("", False),
        # Must not cross-match the sibling heading or vice versa.
        ("DELIVERABLE: SNIPPET", False),
        ("I decided no change is needed here, but did not use the heading.", False),
    ],
)
def test_is_no_change_needed_requires_a_real_standalone_heading(output, expected):
    from bloy_dev_agent.features.twenty import mapping

    assert mapping.is_no_change_needed(output) is expected
    # The two headings are found live to be genuinely distinct outcomes —
    # never let one heading's regex accidentally match the other's text.
    if expected:
        assert mapping.is_snippet_deliverable(output) is False


# ---------------------------------------------------------------------------
# Staging-verify: touches_ui_repo, and build_prompt's staging block
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "repos, expected",
    [
        (["shopify-app-loyalty-cms"], True),
        (["shopify-app-loyalty-api", "shopify-app-loyalty-cms"], True),
        (["shopify-app-loyalty-api"], False),
        ([], False),
    ],
)
def test_touches_ui_repo_only_true_for_a_ui_capable_repo(repos, expected):
    from bloy_dev_agent.features.twenty import mapping

    assert mapping.touches_ui_repo(repos) is expected


def _sample_issue():
    from bloy_dev_agent.features.twenty import mapping

    return mapping.normalize_issue(
        {
            "id": "x",
            "issueKey": "BLOY-9",
            "title": "BLS-9001: Fix a thing",
            "description": "Do the thing",
        }
    )


def test_build_prompt_without_staging_never_mentions_it():
    """The default call (no staging=, or staging=None) must be exactly what
    every ticket got before this feature existed — no trace of the staging
    block anywhere in the output."""
    from bloy_dev_agent.features.twenty import mapping

    issue = _sample_issue()
    default_call = mapping.build_prompt(
        issue, "/worktrees/x", implement=True, monorepo="/monorepo"
    )
    explicit_none = mapping.build_prompt(
        issue, "/worktrees/x", implement=True, monorepo="/monorepo", staging=None
    )

    assert default_call == explicit_none
    for marker in ("Staging verify", "ĐÃ VERIFY TRÊN STAGING", "BLOY_STAGING_TOKEN"):
        assert marker not in default_call


def test_build_prompt_with_staging_appends_without_disturbing_the_rest():
    """The staging block must be a pure addition at the end — the part every
    ordinary ticket already gets must come through completely unchanged."""
    from bloy_dev_agent.features.twenty import mapping

    issue = _sample_issue()
    without_staging = mapping.build_prompt(
        issue, "/worktrees/x", implement=True, monorepo="/monorepo"
    )
    with_staging = mapping.build_prompt(
        issue, "/worktrees/x", implement=True, monorepo="/monorepo",
        staging=mapping.StagingContext(
            control_base_url="https://staging-control.example",
            artifacts_dir="/worktrees/.bloy-artifacts/run-1",
        ),
    )

    assert with_staging.startswith(without_staging)
    added = with_staging[len(without_staging):]
    assert "https://staging-control.example/v1/deploy" in added
    assert "/worktrees/.bloy-artifacts/run-1" in added
    assert "ĐÃ VERIFY TRÊN STAGING" in added
    assert "BLOY_STAGING_TOKEN" in added


def test_build_prompt_staging_block_tells_the_agent_it_may_skip_verification():
    """The agent must judge for itself whether its change is UI-visible — a
    logic-only change inside a UI-capable repo is a legitimate reason to skip
    deploy+screenshot, not a failure."""
    from bloy_dev_agent.features.twenty import mapping

    prompt = mapping.build_prompt(
        _sample_issue(), "/worktrees/x", implement=True, monorepo="/monorepo",
        staging=mapping.StagingContext(
            control_base_url="https://staging-control.example",
            artifacts_dir="/worktrees/.bloy-artifacts/run-1",
        ),
    )

    assert "Decide for yourself" in prompt
    assert "correct outcome, not a failure" in prompt


def test_build_prompt_omits_storefront_section_when_blank():
    """storefront_url="" (the default) must add nothing — graceful absence,
    same pattern as skill_packs_root/monorepo_mirror elsewhere."""
    from bloy_dev_agent.features.twenty import mapping

    prompt = mapping.build_prompt(
        _sample_issue(), "/worktrees/x", implement=True, monorepo="/monorepo",
        staging=mapping.StagingContext(
            control_base_url="https://staging-control.example",
            artifacts_dir="/worktrees/.bloy-artifacts/run-1",
        ),
    )

    assert "Storefront verify" not in prompt


def test_build_prompt_appends_storefront_section_when_configured():
    from bloy_dev_agent.features.twenty import mapping

    issue = _sample_issue()
    without_storefront = mapping.build_prompt(
        issue, "/worktrees/x", implement=True, monorepo="/monorepo",
        staging=mapping.StagingContext(
            control_base_url="https://staging-control.example",
            artifacts_dir="/worktrees/.bloy-artifacts/run-1",
        ),
    )
    with_storefront = mapping.build_prompt(
        issue, "/worktrees/x", implement=True, monorepo="/monorepo",
        staging=mapping.StagingContext(
            control_base_url="https://staging-control.example",
            artifacts_dir="/worktrees/.bloy-artifacts/run-1",
            storefront_url="https://test-bloy-loyalty.myshopify.com",
            storefront_password="1",
        ),
    )

    assert with_storefront.startswith(without_storefront)
    added = with_storefront[len(without_storefront):]
    assert "https://test-bloy-loyalty.myshopify.com" in added
    assert "`1`" in added
    assert "/worktrees/.bloy-artifacts/run-1" in added
    assert "NOT a real credential" in added


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
