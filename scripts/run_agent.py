#!/usr/bin/env python3
"""Run one Twenty issue through the Dev Agent from the command line.

Same code path the routine action uses — this is the manual entry point for
trying a single issue without waiting for a schedule.

Usage (from the agent-manager project root)::

    PYTHONPATH=community_plugins uv run python \
        community_plugins/bloy_dev_agent/scripts/run_agent.py BLOY-2 \
        --repo /home/bss-group/BLOY

Environment:
    BLOY_TWENTY_BASE_URL / BLOY_TWENTY_API_KEY  the workspace to read/write
    BLOY_CLAUDE_BIN                             path to the claude CLI
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from bloy_dev_agent.features import runner
from bloy_dev_agent.features.twenty.client import TwentyClient, TwentyError


def load_project_env() -> None:
    """Read agent-manager's .env so credentials live in one place.

    The plugin is normally installed as a symlink into ``community_plugins``,
    so resolving ``__file__`` walks out of the install tree; search up from the
    working directory instead.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for directory in [Path.cwd(), *Path.cwd().parents]:
        env_file = directory / ".env"
        if env_file.is_file():
            load_dotenv(env_file)
            return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("issue_key", help="Issue key, e.g. BLOY-2")
    parser.add_argument("--repo", default="/home/bss-group/BLOY")
    parser.add_argument("--timeout", type=int, default=runner.core_services.DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--implement",
        action="store_true",
        help="Let the agent edit the repository instead of only reading it",
    )
    parser.add_argument(
        "--keep-status",
        action="store_true",
        help="Do not move the issue between columns",
    )
    args = parser.parse_args()

    load_project_env()
    base_url = os.environ.get("BLOY_TWENTY_BASE_URL", "").strip()
    api_key = os.environ.get("BLOY_TWENTY_API_KEY", "").strip()
    if not base_url or not api_key:
        print("Set BLOY_TWENTY_BASE_URL and BLOY_TWENTY_API_KEY first.")
        return 2

    repo = Path(args.repo)
    if not repo.is_dir():
        print(f"Repository path does not exist: {repo}")
        return 2

    client = TwentyClient(base_url=base_url, api_key=api_key)
    try:
        records = client.list_records(
            "issues", filter_expression=f'issueKey[eq]:"{args.issue_key}"', limit=1, depth=1
        )
        if not records:
            print(f"No issue with key {args.issue_key}")
            return 1
        record = records[0]
        statuses = (
            {}
            if args.keep_status
            else runner._statuses_by_name(client, str(record.get("projectId") or ""))
        )
    except TwentyError as exc:
        print(f"Twenty: {exc.message}")
        return 1

    print(f"→ {record.get('issueKey')}: {record.get('title')}")
    outcome = runner.run_issue(
        client,
        record,
        repo=repo,
        statuses=statuses,
        timeout=args.timeout,
        implement=args.implement,
    )
    print(
        f"  ok={outcome.ok} channel={outcome.channel or '-'} "
        f"chars={outcome.output_chars} moved_to={outcome.moved_to or '-'}"
    )
    if not outcome.ok:
        print(f"  {outcome.detail}")
    return 0 if outcome.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
