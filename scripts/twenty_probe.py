#!/usr/bin/env python3
"""Probe a Twenty workspace with the plugin's own client.

Answers the questions the sync needs before any mapping can be written: which
objects exist, which of them are custom, what fields they carry, and what a
real record looks like.

Usage (from the agent-manager project root)::

    BLOY_TWENTY_BASE_URL=http://localhost:3010 \
    BLOY_TWENTY_API_KEY=<key> \
    PYTHONPATH=community_plugins uv run python \
        community_plugins/bloy_dev_agent/scripts/twenty_probe.py [objectNamePlural]

Passing an object name also dumps one record from it, so the payload shape is
observed rather than guessed.
"""

from __future__ import annotations

import json
import os
import sys

from bloy_dev_agent.features.twenty.client import TwentyClient, TwentyError


def main() -> int:
    base_url = os.environ.get("BLOY_TWENTY_BASE_URL", "").strip()
    api_key = os.environ.get("BLOY_TWENTY_API_KEY", "").strip()
    if not base_url or not api_key:
        print("Set BLOY_TWENTY_BASE_URL and BLOY_TWENTY_API_KEY first.")
        return 2

    client = TwentyClient(base_url=base_url, api_key=api_key)

    try:
        objects = client.describe_objects()
    except TwentyError as exc:
        print(f"Metadata query failed: {exc.message}")
        return 1

    custom = [o for o in objects if o.is_custom]
    standard = [o for o in objects if not o.is_custom]
    print(f"{len(objects)} active objects — {len(custom)} custom, {len(standard)} standard\n")

    for obj in custom + standard:
        marker = "custom  " if obj.is_custom else "standard"
        print(f"[{marker}] {obj.name_plural:<28} ({obj.label_singular})")
        print(f"             {', '.join(obj.field_names())}\n")

    target = sys.argv[1] if len(sys.argv) > 1 else None
    if not target:
        print("Pass an objectNamePlural to dump one record from it.")
        return 0

    try:
        records = client.list_records(target, limit=1, depth=1)
    except TwentyError as exc:
        print(f"Could not read {target}: {exc.message}")
        return 1

    if not records:
        print(f"{target} has no records yet.")
        return 0

    print(f"--- one record from {target} ---")
    print(json.dumps(records[0], indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
