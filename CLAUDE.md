# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
uv sync

# Run the server (development)
uv run uvicorn main:app --reload

# Add a dependency
uv add <package>

# Run tests / lint / type check
uv run pytest
uv run ruff check .
uv run mypy .
```

## Architecture

FastAPI tile server for IMOS ocean data products. Entry point is `main.py`. All tile serving goes through a Zarr-backed stack.

If you any specific knowledge about this project, see `docs/technical.md` for full details.
