# One MCP server, many agent clients: per-client config matrix (verified 2026-09-18)

Same stdio server (`uv run --project tools/kicad-mcp kicad-mcp`) registered in each client:

| Client | File | Root key | stdio `type` | Verified |
|---|---|---|---|---|
| Claude Code CLI + VS Code ext (project) | `<repo>/.mcp.json` | `mcpServers` | `"stdio"` | locally (claude 2.1.276 `mcp list` -> Connected) |
| Claude Code (user) | `~/.claude.json` via `claude mcp add --scope user` | `mcpServers` | `"stdio"` | docs |
| VS Code (Copilot, Local harness) | `.vscode/mcp.json` or user `mcp.json` | `servers` (+`inputs`,`sandbox`) | `"stdio"` | docs |
| VS Code Agent Host (Copilot harness) | reads `<repo>/.mcp.json` natively + forwards `.vscode/mcp.json` | `mcpServers` | `"stdio"` | docs |
| Copilot CLI | `~/.copilot/mcp-config.json` | `mcpServers` | `"local"` + `"tools": ["*"]` | docs |
| Cursor | `.cursor/mcp.json` (project) / `~/.cursor/mcp.json` | `mcpServers` | `"stdio"` (optional) | docs |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | `mcpServers` | (none; `serverUrl` for remote) | docs |
| Codex CLI / Codex VS Code ext / ChatGPT app | `~/.codex/config.toml` or `<repo>/.codex/config.toml` | `[mcp_servers.<name>]` | n/a (TOML) | docs |
| Any VS Code extension (programmatic) | `vscode.lm.registerMcpServerDefinitionProvider` + `contributes.mcpServerDefinitionProviders` | n/a | `McpStdioServerDefinition` | d.ts 1.138 |

See the sibling files for exact content.
