# SearchCAIE MCP Server

Standalone MCP server for Search CAIE past-paper search.

## Features

- Core search tools for single-topic and multi-topic queries
- Filters for subject, paper, year, session, chapter, mode, and pagination
- Batch question fetch with full mark scheme details
- Upstream retries and structured error payloads

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
        "MCP_API_BASE": "https://api.searchcaie.qzz.io/api",
        "MCP_DEFAULT_SUBJECT": "9618"
      }
    }
  }
}
```

## Environment variables

- `MCP_API_BASE` (default: `https://api.searchcaie.qzz.io/api`)
- `MCP_DEFAULT_SUBJECT` (default: `9618`)
- `MCP_REQUEST_TIMEOUT` (default: `30`)
- `MCP_TRANSPORT` (default: `stdio`)
- `MCP_HOST` (default: `127.0.0.1`)
- `MCP_PORT` (default: `8000`)
- `MCP_PATH` (default: `/mcp`)

## Run directly

```bash
searchcaie-mcp
```
