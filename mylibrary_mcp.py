#!/usr/bin/env python3
"""
MyLibrary Model Context Protocol (MCP) Server (`mylibrary_mcp.py`)
==================================================================
Exposes all MyLibrary Catalog, Pipeline Status, and epubmeta Enrichment functions
as MCP Tools and Resources for AI agents (Antigravity, Claude Desktop, Cursor, etc.).

Works zero-dependency over stdio using built-in JSON-RPC 2.0 MCP protocol (or
via `mcp.server.fastmcp.FastMCP` if installed).

MCP Configuration Example (`mcp_config.json` / `claude_desktop_config.json`):
{
  "mcpServers": {
    "mylibrary": {
      "command": "python",
      "args": ["c:\\Users\\oscar\\Repo_Pers\\notgithub\\MyLibrary\\mylibrary_mcp.py"],
      "env": {
        "MYLIBRARY_URL": "http://192.168.1.43:8080/mylibrary",
        "MYLIBRARY_PATH_PREFIX": "U:\\\\Media"
      }
    }
  }
}
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

from mylibrary_cli import DEFAULT_BASE_URL, DEFAULT_PATH_PREFIX, MyLibraryClient

SERVER_NAME = "mylibrary-mcp"
SERVER_VERSION = "1.0.0"

client = MyLibraryClient(
    base_url=os.environ.get("MYLIBRARY_URL", DEFAULT_BASE_URL),
    path_prefix=os.environ.get("MYLIBRARY_PATH_PREFIX", DEFAULT_PATH_PREFIX),
)


# ==============================================================================
# Tool Definitions & Execution Dispatcher
# ==============================================================================

TOOLS_SPEC: List[Dict[str, Any]] = [
    {
        "name": "mylibrary_get_status",
        "description": (
            "Get live MyLibrary pipeline status, scanner state (Active/Standby/Idle), "
            "next scheduled scan timestamp and countdown, remaining files to catalog, "
            "remaining EPUBs to enrich, current cycle lookup progress, and BookDB/SQLite KPIs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "mylibrary_search_catalog",
        "description": (
            "Search and filter the 1.07M+ item MyLibrary BookDB catalog by keyword query "
            "(title, author, ISBN, description), item type, file format, exact ISBN/ASIN, or author. "
            "Returns matching books with mapped NAS paths (e.g. U:\\Media\\books\\...)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (title, author, ISBN, synopsis)"},
                "item_type": {
                    "type": "string",
                    "description": "Optional item type filter: BOOK, MAGAZINE, KINDLE, AUDIBLE, AUDIOBOOK",
                },
                "file_format": {
                    "type": "string",
                    "description": "Optional file format filter: EPUB, PDF, MOBI, AZW3, MP3, M4B",
                },
                "isbn": {"type": "string", "description": "Optional exact ISBN or ASIN filter"},
                "author": {"type": "string", "description": "Optional author substring filter"},
                "limit": {"type": "integer", "description": "Max items to return (default 20, max 200)", "default": 20},
                "offset": {"type": "integer", "description": "Pagination offset (default 0)", "default": 0},
            },
        },
    },
    {
        "name": "mylibrary_get_book",
        "description": (
            "Retrieve full details for a single book in MyLibrary by UUID, ISBN, ASIN, SHA-256 hash, "
            "or file path, including synopsis, all physical file locations, and epubmeta enrichment history."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "identifier": {
                    "type": "string",
                    "description": "Book UUID, ISBN, ASIN, SHA-256 file hash, or file path",
                },
            },
            "required": ["identifier"],
        },
    },
    {
        "name": "mylibrary_update_book",
        "description": "Update a book's metadata fields (title, authors, publisher, isbn, asin, description) in BookDB.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "identifier": {"type": "string", "description": "Book UUID, ISBN, or ASIN"},
                "title": {"type": "string", "description": "Updated title"},
                "authors": {"type": "string", "description": "Updated author(s)"},
                "publisher": {"type": "string", "description": "Updated publisher"},
                "isbn": {"type": "string", "description": "Updated ISBN"},
                "asin": {"type": "string", "description": "Updated ASIN"},
                "description": {"type": "string", "description": "Updated description/synopsis"},
            },
            "required": ["identifier"],
        },
    },
    {
        "name": "mylibrary_suggest",
        "description": "Get fast autocomplete title/author suggestions for a search prefix.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search prefix (at least 2 characters)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "mylibrary_list_enrichments",
        "description": (
            "List and search epubmeta conversion/enrichment records from scanner_state.db "
            "with Before vs. After metadata comparison (title, author, publisher, ISBN, description, cover). "
            "Filter by status: NEEDS_REVIEW, QUARANTINED, NO_ISBN, SUCCESS, FAILED, IGNORED, or ALL."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["ALL", "SUCCESS", "NEEDS_REVIEW", "NO_ISBN", "QUARANTINED", "FAILED", "IGNORED"],
                    "default": "NEEDS_REVIEW",
                    "description": "Enrichment status filter",
                },
                "search": {
                    "type": "string",
                    "description": "Optional search filter across filename, path, title, author, ISBN, or diagnostic note",
                },
                "limit": {"type": "integer", "default": 25, "description": "Max records to return (default 25)"},
                "offset": {"type": "integer", "default": 0, "description": "Pagination offset (default 0)"},
            },
        },
    },
    {
        "name": "mylibrary_approve_enrichment",
        "description": (
            "Approve a NEEDS_REVIEW or NO_ISBN enrichment item in scanner_state.db and BookDB. "
            "Supports full candidate approval or mix-and-match field selection (passing custom/original "
            "title, author, publisher, isbn, description, or cover)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Container path (/books/...) or mapped path (U:\\Media\\books\\...)",
                },
                "title": {"type": "string", "description": "Optional selected/edited title to save"},
                "author": {"type": "string", "description": "Optional selected/edited author to save"},
                "publisher": {"type": "string", "description": "Optional selected/edited publisher to save"},
                "isbn": {"type": "string", "description": "Optional selected/edited ISBN to save"},
                "description": {"type": "string", "description": "Optional selected/edited description to save"},
                "cover": {"type": "string", "description": "Optional selected cover URL or data URI to save"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "mylibrary_ignore_enrichment",
        "description": "Keep original file metadata and mark an enrichment item as IGNORED so it is not flagged for review.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Container path (/books/...) or mapped path (U:\\Media\\books\\...)",
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "mylibrary_requeue_enrichment",
        "description": "Remove a file from processed_files in scanner_state.db so epubmeta re-scans it on the next cycle.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Container path (/books/...) or mapped path (U:\\Media\\books\\...)",
                },
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "mylibrary_get_enrichment_history",
        "description": (
            "Get hourly or daily time-series history (both per-period rate and cumulative totals) "
            "for ALL, SUCCESS, NEEDS_REVIEW, NO_ISBN, QUARANTINED, FAILED, IGNORED, and CACHE."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "granularity": {
                    "type": "string",
                    "enum": ["hour", "day"],
                    "default": "hour",
                    "description": "Time bucket granularity ('hour' or 'day')",
                },
            },
        },
    },
    {
        "name": "mylibrary_get_logs",
        "description": "Tail live scanner.log and/or epubmeta.log with optional keyword filtering.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["scanner", "epubmeta", "both"],
                    "default": "both",
                    "description": "Which log file(s) to tail",
                },
                "lines": {"type": "integer", "default": 50, "description": "Number of lines to return (max 500)"},
                "grep": {"type": "string", "description": "Optional case-insensitive filter substring"},
            },
        },
    },
]


def execute_tool(name: str, arguments: Dict[str, Any]) -> Any:
    args = arguments or {}
    if name == "mylibrary_get_status":
        return client.get_status()
    if name == "mylibrary_search_catalog":
        return client.search_catalog(
            query=args.get("query", ""),
            item_type=args.get("item_type"),
            file_format=args.get("file_format"),
            isbn=args.get("isbn"),
            author=args.get("author"),
            limit=int(args.get("limit", 20)),
            offset=int(args.get("offset", 0)),
        )
    if name == "mylibrary_get_book":
        return client.get_book(args["identifier"])
    if name == "mylibrary_update_book":
        updates = {
            k: args[k]
            for k in ("title", "authors", "publisher", "isbn", "asin", "description")
            if k in args and args[k] is not None
        }
        return client.update_book(args["identifier"], updates)
    if name == "mylibrary_suggest":
        return client.suggest(args["query"])
    if name == "mylibrary_list_enrichments":
        return client.list_enrichments(
            status=args.get("status", "NEEDS_REVIEW"),
            search=args.get("search", ""),
            limit=int(args.get("limit", 25)),
            offset=int(args.get("offset", 0)),
        )
    if name == "mylibrary_approve_enrichment":
        selected = {
            k: args[k]
            for k in ("title", "author", "publisher", "isbn", "description", "cover")
            if k in args and args[k] is not None
        }
        return client.approve_enrichment(
            args["file_path"],
            selected_metadata=selected if selected else None,
        )
    if name == "mylibrary_ignore_enrichment":
        return client.ignore_enrichment(args["file_path"])
    if name == "mylibrary_requeue_enrichment":
        return client.requeue_enrichment(args["file_path"])
    if name == "mylibrary_get_enrichment_history":
        return client.get_history(granularity=args.get("granularity", "hour"))
    if name == "mylibrary_get_logs":
        return client.get_logs(
            source=args.get("source", "both"),
            lines=int(args.get("lines", 50)),
            grep=args.get("grep", ""),
        )
    raise ValueError(f"Unknown tool: {name}")


# ==============================================================================
# Zero-Dependency JSON-RPC 2.0 Stdio MCP Loop
# ==============================================================================

def _send_response(msg_id: Any, result: Any = None, error: Optional[Dict[str, Any]] = None) -> None:
    payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def handle_rpc_message(msg: Dict[str, Any]) -> None:
    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    # Notifications have no id
    if msg_id is None:
        return

    if method == "initialize":
        protocol_version = params.get("protocolVersion", "2024-11-05")
        _send_response(
            msg_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                },
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
            },
        )
    elif method == "ping":
        _send_response(msg_id, {})
    elif method == "tools/list":
        _send_response(msg_id, {"tools": TOOLS_SPEC})
    elif method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments") or {}
        try:
            res = execute_tool(tool_name, tool_args)
            _send_response(
                msg_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(res, indent=2),
                        }
                    ],
                    "isError": False,
                },
            )
        except Exception as exc:
            _send_response(
                msg_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"ok": False, "error": str(exc)}, indent=2),
                        }
                    ],
                    "isError": True,
                },
            )
    elif method == "resources/list":
        _send_response(
            msg_id,
            {
                "resources": [
                    {
                        "uri": "mylibrary://status",
                        "name": "MyLibrary Pipeline & Catalog Status",
                        "description": "Live scanner state, next scan schedule, remaining queues, and BookDB/epubmeta KPIs",
                        "mimeType": "application/json",
                    },
                    {
                        "uri": "mylibrary://history/hourly",
                        "name": "MyLibrary Hourly Enrichment History",
                        "description": "Hourly time-series counts of epubmeta enrichments and metadata cache",
                        "mimeType": "application/json",
                    },
                ]
            },
        )
    elif method == "resources/read":
        uri = params.get("uri", "")
        try:
            if uri == "mylibrary://status":
                data = client.get_status()
            elif uri == "mylibrary://history/hourly":
                data = client.get_history("hour")
            else:
                raise ValueError(f"Unknown resource URI: {uri}")
            _send_response(
                msg_id,
                {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json",
                            "text": json.dumps(data, indent=2),
                        }
                    ]
                },
            )
        except Exception as exc:
            _send_response(msg_id, error={"code": -32602, "message": str(exc)})
    else:
        _send_response(msg_id, error={"code": -32601, "message": f"Method not found: {method}"})


def main() -> None:
    try:
        if hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            if isinstance(msg, dict):
                handle_rpc_message(msg)
        except Exception as exc:
            sys.stderr.write(f"[mylibrary-mcp] Error parsing message: {exc}\n")
            sys.stderr.flush()


if __name__ == "__main__":
    main()
