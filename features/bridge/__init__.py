"""The only package allowed to reach into the agent_team plugin.

Every other module in this plugin must go through ``agent_team.py`` here. A
test asserts that no other file imports ``agent_team`` so the boundary stays
real when the documentation is forgotten.
"""
