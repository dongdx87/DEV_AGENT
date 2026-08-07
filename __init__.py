"""BLOY Dev Agent plugin.

Connects Twenty (task source) to the Agent Team board so BLOY development
tasks can be picked up and executed by agents, and provides BLOY-specific
capabilities (Shopify session capture, skill pack) to those agents.

Layout:
    features/twenty/   Twenty REST/GraphQL client, mapping, sync
    features/bridge/   The ONLY place allowed to talk to the agent_team plugin
"""
