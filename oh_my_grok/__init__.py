"""oh-my-grok: SQLite-backed multi-Bot orchestration engine for Grok Bot.

Layers: control plane (MCP server `omg` + CLI `omg`; `omoctl` execs that CLI), orchestration engine
(this package's `engine`), wake-up plane (`dispatcher`).
"""
__version__ = "0.1.0"
SCHEMA_VERSION_PAYLOAD = 1
