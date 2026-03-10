# SearchCAIE MCP Server

Standalone MCP server for Search CAIE past-paper search.

## Features

- Core search tools for single-topic and multi-topic queries
- Filters for subject, paper, year, session, chapter, mode, and pagination
- LLM-friendly tool responses: concise text preview plus structured JSON
- Multi-topic search returns `recommended_ids` for quick follow-up retrieval
- `get_questions` supports both `compact` (default) and `full` detail modes
- Backward-compatible inputs: comma-separated strings and native arrays
- Upstream retries and structured error handling

## Tool behavior notes

- `search_multi` accepts either `topics` (comma-separated string) or `topics_list` (array)
- `get_questions` accepts either `question_ids` (comma-separated string) or `question_ids_list` (array)
- `get_questions` defaults to `detail="compact"` to reduce token usage and improve LLM answer quality

## Install

```bash
pip install .
```

Or from git:

```bash
pip install "git+https://github.com/Pixel2075/searchcaie-mcp.git"
```

## Claude Desktop config

```json
{
  "mcpServers": {
    "searchcaie-search": {
      "command": "searchcaie-mcp",
      "env": {
        "MCP_API_BASE": "https://api.searchcaie.com/api"
      }
    }
  }
}
```

`MCP_DEFAULT_SUBJECT` is optional. If omitted, the server does not apply a
subject filter by default.

## Environment variables

- `MCP_API_BASE` (default: `https://api.searchcaie.com/api`)
- `MCP_DEFAULT_SUBJECT` (optional; if unset, no default subject filter is applied)
- `MCP_REQUEST_TIMEOUT` (default: `30`)
- `MCP_TRANSPORT` (default: `stdio`)
- `MCP_HOST` (default: `127.0.0.1`)
- `MCP_PORT` (default: `8000`)
- `MCP_PATH` (default: `/mcp`)

## Run directly

```bash
searchcaie-mcp
```

## Run as a remote MCP server

```bash
MCP_TRANSPORT=streamable-http \
MCP_HOST=0.0.0.0 \
MCP_PORT=8000 \
MCP_PATH=/mcp \
searchcaie-mcp
```
