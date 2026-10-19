# multi_agent_ai_systems

A shared data gateway for many AI agents. Agents reach a rate-limited, changing data source
through one MCP server that merges duplicate requests, shares the rate limit, and labels
every answer with how fresh it is. The first data source is SEC EDGAR.

Status: early development.

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```
uv sync
cp .env.example .env   # then put your name and email in .env
```

## Tests

```
uv run pytest              # offline tests only
uv run pytest -m live      # tests that call the real SEC API
```