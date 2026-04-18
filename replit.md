# Workspace

## Overview

pnpm workspace monorepo using TypeScript. Each package manages its own dependencies.
Also includes a Python-based Vertex AI Proxy service.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)

## Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- `pnpm --filter @workspace/api-server run dev` — run API server locally

See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details.

## Vertex AI Proxy (Python)

- **Location**: `artifacts/vertex-proxy/`
- **Language**: Python 3.11
- **Framework**: FastAPI + uvicorn
- **Port**: 8000
- **Start command**: `cd artifacts/vertex-proxy && PORT=8000 python3 main.py`
- **Health check**: `GET /health`
- **API**: OpenAI-compatible at `/v1/chat/completions`, Gemini at `/v1beta/models/{model}:generateContent`
- **Config**: `artifacts/vertex-proxy/config/config.json`
- **API keys**: `artifacts/vertex-proxy/config/api_keys.txt`
- **Dependencies**: fastapi, uvicorn, pydantic, primp (Rust static TLS), httpx, beautifulsoup4, lxml, pyyaml
- **Proxy**: Uses xray binary (`artifacts/vertex-proxy/bin/xray`) for SOCKS5 proxy rotation
- **Source**: Pulled from https://github.com/tian13669035950-byte/a
