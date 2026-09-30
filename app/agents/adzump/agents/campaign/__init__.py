"""The Campaign sub-agent: builds an ad campaign from a finalized product brief.

``CampaignAgent`` (``agent.py``) is a platform-agnostic BaseAgent shell that runs the
selected platform's creation tools (``tools/``); the Google Search path runs one keyword
research pass per ad group the user chose. See ./AGENT.md for the flow.
"""
