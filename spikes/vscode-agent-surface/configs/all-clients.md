# Exact config snippets

## `<repo>/.mcp.json`  (Claude Code project scope; also read natively by VS Code Agent Host)
```json
{
  "mcpServers": {
    "kicad": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--project", "${CLAUDE_PROJECT_DIR}/tools/kicad-mcp", "kicad-mcp"],
      "env": { "KICAD_ROOT": "C:\\Program Files\\KiCad" }
    }
  }
}
```
Auto-approve for the team: `.claude/settings.json` -> `{"enableAllProjectMcpServers": true}` (in this test only the USER-level settings file made `claude mcp list` report Connected; the project-level file left it "Pending approval" for the non-interactive `mcp list` health check).

## `.vscode/mcp.json`  (VS Code native; NOTE root key `servers`)
```json
{
  "servers": {
    "kicad": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--project", "${workspaceFolder}/tools/kicad-mcp", "kicad-mcp"],
      "cwd": "${workspaceFolder}",
      "env": { "KICAD_ROOT": "C:\\Program Files\\KiCad" },
      "dev": { "watch": "tools/kicad-mcp/**/*.py", "debug": { "type": "python" } }
    }
  },
  "inputs": []
}
```

## `.cursor/mcp.json`  (Cursor project) / `~/.cursor/mcp.json` (global) - 40-tool cap
```json
{ "mcpServers": { "kicad": { "command": "uv", "args": ["run", "--project", "${workspaceFolder}/tools/kicad-mcp", "kicad-mcp"], "env": { "KICAD_ROOT": "C:\\Program Files\\KiCad" } } } }
```

## `~/.codeium/windsurf/mcp_config.json`  (Windsurf; user-level only; 100-tool cap; remote uses `serverUrl`)
```json
{ "mcpServers": { "kicad": { "command": "uv", "args": ["run", "--project", "C:/abs/path/tools/kicad-mcp", "kicad-mcp"], "env": { "KICAD_ROOT": "C:\\Program Files\\KiCad" } } } }
```

## `~/.copilot/mcp-config.json`  (Copilot CLI + Agent Host user scope; `type: local`, `tools` allowlist)
```json
{ "mcpServers": { "kicad": { "type": "local", "command": "uv", "args": ["run", "--project", "C:/abs/path/tools/kicad-mcp", "kicad-mcp"], "env": { "KICAD_ROOT": "C:\\Program Files\\KiCad" }, "tools": ["*"] } } }
```

## `~/.codex/config.toml` or `<repo>/.codex/config.toml`  (Codex CLI / Codex VS Code ext / ChatGPT desktop share it)
```toml
[mcp_servers.kicad]
command = "uv"
args = ["run", "--project", "C:/abs/path/tools/kicad-mcp", "kicad-mcp"]
startup_timeout_sec = 30
tool_timeout_sec = 600
[mcp_servers.kicad.env]
KICAD_ROOT = "C:\\Program Files\\KiCad"
```
CLI equivalents: `claude mcp add --scope project kicad -e KICAD_ROOT="C:\Program Files\KiCad" -- uv run --project tools/kicad-mcp kicad-mcp`,
`codex mcp add kicad -- uv run --project tools/kicad-mcp kicad-mcp`,
`code --add-mcp "{\"name\":\"kicad\",\"command\":\"uv\",\"args\":[\"run\",\"--project\",\"tools/kicad-mcp\",\"kicad-mcp\"]}"`.
