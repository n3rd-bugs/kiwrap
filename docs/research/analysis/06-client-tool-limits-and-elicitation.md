# 06: client tool limits and MCP elicitation (Claude Code in VS Code)

Date: 2026-09-24. Track: `client-limits`. This answers two open owner questions:

- Decision 4 of `requests/kiwrap-feature-requests.review.md` §8: "Can we register only 36 tools? If there
  is a restriction, we will likely need to wrap the tool calls."
- The consent prompt in D21: "MCP elicitation where the client supports it, otherwise a teaching error the
  agent relays, and a modal in the VS Code extension."

D18 fixes the client to **Claude Code inside VS Code on Windows**. Other clients are listed only for
completeness.

Measured on this machine:

- Claude Code **2.1.280**, the build bundled in the VS Code extension
  `anthropic.claude-code-2.1.280-win32-x64`. The latest public release is 2.1.281 (2026-09-23).
- Python `mcp` **2.2.0**, in the kiwrap env and in a scratch venv.
- The kiwrap server as of `aa939ea`.

Scripts and logs are under
`%TEMP%/claude/c--Upwork-KiCad/48a8df66-…/scratchpad/tooling/client-limits/`:

| File | What it is |
|---|---|
| `many_tools.py` | 150-tool stdio server |
| `elicit_server.py` | Elicitation probe server |
| `probe_client.py`, `nocap.py` | Python-SDK clients |
| `tap.py` | JSON-RPC wiretap |
| `tap_*.log`, `debug1.log` | Claude Code's side of the handshake |
| `tools_kiwrap.json` | kiwrap's `tools/list` |

`[MEASURED]` marks results reproduced here. Every other claim cites its source.

## Answer first

1. **Claude Code sets no tool-count limit, per server or in total.** The docs say so ("Claude Code
   doesn't impose a fixed per-server tool cap; the practical limit is your context window budget"). No
   cap appears in the 2.1.280 binary `[MEASURED]`. Claude Code accepted a 150-tool server in one
   `tools/list` `[MEASURED]`. MCP tools are **deferred by default**: only names and server instructions
   are in context, and each schema is loaded through `ToolSearch` when needed.
   - **Do not wrap kiwrap's tools in a dispatcher.**
   - Treat "36" as a design guideline, not a limit. It came from Cursor's 40-tool cap
     (research/06), and D18 made that cap irrelevant.
   - Keep about 30–45 coherent, enum-parameterised `kw_*` tools. Spend the effort on names,
     descriptions and schema size instead.
2. **Claude Code supports MCP form elicitation**:
   - Since **v2.1.76** (changelog). It shows an interactive dialog in the terminal UI.
   - URL mode needs the **2026-07-28** protocol and v2.1.281.
   - The `Elicitation` / `ElicitationResult` hooks can answer or override it.
   - **The default VS Code chat panel does not show it (2.1.280).** The panel is an Agent-SDK host that
     does not register an `onElicitation` handler. The SDK therefore answers every elicitation with
     `{"action":"decline"}`. The kiwrap server cannot tell that answer apart from a user's refusal.
     This comes from static analysis of the shipped bundle. It could not be run end to end: no
     credentials were available under a scratch config dir.
   - The CLI still advertises `capabilities.elicitation: {}` `[MEASURED]`, so a capability check does
     not detect the problem.
   - The `mcp` 2.2 SDK exposes `ctx.elicit()` / `ctx.elicit_url()` and a resolver API (`Resolve` +
     `Elicit`).
     - `ctx.elicit()` **fails on 2026-07-28 connections** (`NoBackChannelError`) `[MEASURED]`.
     - The resolver API works on both protocol eras `[MEASURED]`.
3. **For D21 the robust consent gate is a Claude Code permission prompt, not elicitation.**
   - Put the upgrade in its own tool that carries `_meta["anthropic/requiresUserInteraction"]: true`.
     Claude Code then asks the human on **every** call, in every permission mode including bypass, and
     never offers "don't ask again". This works in the panel and in the CLI (documented since v2.1.199;
     the key is present in the 2.1.280 binary `[MEASURED]`).
   - Keep the teaching error as the universal fallback.
   - Use form elicitation only where a human can see it. Treat `decline` and `cancel` as "no answer",
     not as a refusal.

## 1. Tool count

### 1.1 What each client and layer allows

| Layer / client | Tool limit | How many tools reach the model | Source |
|---|---|---|---|
| MCP spec 2025-11-25 | none; `tools/list` is paginated (`cursor` / `nextCursor`); names SHOULD be 1–128 chars `[A-Za-z0-9_.-]` | n/a | [spec: tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools) |
| Claude Code CLI + VS Code panel (2.1.280) | **none**; "Claude Code doesn't impose a fixed per-server tool cap; the practical limit is your context window budget" | **deferred by default**: names + server instructions up front; `ToolSearch` loads schemas (up to 5 per search by default) | [code.claude.com/docs/en/mcp#scale-with-mcp-tool-search](https://code.claude.com/docs/en/mcp) |
| Claude API tool search (what `ToolSearch` rides on) | 10,000 deferred tools per request; each search returns 5 by default (`limit` 1–10,000); regex ≤ 200 chars, BM25 ≤ 500 chars | the 3–5 found per search | [platform.claude.com tool-search-tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool) |
| Agent SDK (the panel is built on it) | "Maximum tools: 10,000 tools in your catalog" | same as above | [code.claude.com/docs/en/agent-sdk/tool-search](https://code.claude.com/docs/en/agent-sdk/tool-search) |
| VS Code Copilot Chat (out of scope, D18) | **128 tools per request**; `github.copilot.chat.virtualTools.threshold` groups extras | ≤ 128 | [VS Code: agent tools](https://code.visualstudio.com/docs/copilot/agents/agent-tools) (updated 2026-09-16) |
| Cursor (out of scope, D18) | 40 tools across all MCP servers, extras silently dropped. Reported in the Cursor forum and in research/06. The current official MCP page states no cap. | ≤ 40 | [forum.cursor.com/t/…/81627](https://forum.cursor.com/t/mcp-server-40-tool-limit-in-cursor-is-this-frustrating-your-workflow/81627), [cursor.com/docs/context/mcp](https://cursor.com/docs/context/mcp) |
| Windsurf (out of scope) | 100 (research/06) | ≤ 100 | `docs/research/06-vscode-agent-surface.md` |

Other Claude Code limits matter more than the count. All are from the
[MCP docs](https://code.claude.com/docs/en/mcp):

- **2,048-character cap on each tool description and on each server's `instructions`**, truncated
  silently. `CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH` changes it from v2.1.280.
  - kiwrap's longest description is 1,049 chars (`kw_pcb_route_auto`); its instructions are 1,195 chars
    `[MEASURED]`. Both fit.
- **Output:** a warning above 10,000 tokens and a 25,000-token cap (`MAX_MCP_OUTPUT_TOKENS`). The
  per-tool `_meta["anthropic/maxResultSizeChars"]` raises the text cap up to 500,000 chars; kiwrap
  already sets it.
- **Schema checks:** Claude Code drops only the individual tool whose input schema fails the API checks.
  - Property names must be 1–64 chars.
  - The schema must be valid draft 2020-12.
  - Root-level `anyOf` / `oneOf` / `allOf` is flattened (v2.1.195+).

### 1.2 How tool search / deferred loading works (2.1.280)

`ENABLE_TOOL_SEARCH` takes these values
([docs](https://code.claude.com/docs/en/mcp#configure-tool-search)):

| `ENABLE_TOOL_SEARCH` | Behaviour |
|---|---|
| unset (default) | **All MCP tools deferred**, loaded on demand |
| `true` | Deferred, even through proxies (requests fail on proxies without `tool_reference` support) |
| `auto` | Loaded up front while the deferrable definitions total < 10% of the context window; all deferred once they reach 10% |
| `auto:N` | Same as `auto` with an N% threshold (0–100) |
| `false` | Everything loaded up front on every turn |

Tool search is **off**, and kiwrap's whole schema set is then paid on every turn, in these cases:

- `ANTHROPIC_BASE_URL` points at a non-first-party proxy, unless `ENABLE_TOOL_SEARCH` is set.
- `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` is set. This cannot be overridden.
- The deployment is Microsoft Foundry on Azure.
- The model is an older (pre-4.5) model on Google's Agent Platform.

Tool search needs Sonnet/Haiku/Opus 4.5 or later.

Selection mechanics:

- The model sees the names of the deferred tools. It calls `ToolSearch` with `select:<names>` or with
  keywords.
- The search matches tool names, descriptions, argument names and argument descriptions
  ([API docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool)).
- The matching definitions are expanded inline as `tool_reference` blocks, so the cached prefix is not
  disturbed. They stay loaded until compaction.
- A server can exempt tools from deferral: `alwaysLoad: true` in the server config, or
  `_meta["anthropic/alwaysLoad"]: true` per tool.
- The 2.1.280 binary also reads an undocumented `_meta["anthropic/searchHint"]`. Do not rely on it.

History from the [changelog](https://code.claude.com/docs/en/changelog):

| Version | Change |
|---|---|
| 2.1.7 (2026-01-14) | Auto mode on for everyone (`MCPSearch`, 10% threshold) |
| 2.1.9 | `auto:N` |
| 2.1.113 | Ranking fix so exact tool names win |
| later | Default switched to "all deferred" (the current docs) |

### 1.3 Local measurements

**A. 150-tool server through the Python SDK.** `many_tools.py` defines 150 tools `t_1..t_150`, each
returning a keyed hash only the real tool can produce.

- `tools/list` returned all 150 in **one page**: `MCPServer` does not paginate.
- `call_tool("t_137", {"x": 1})` returned `t_137 result: 442b0b107e78` `[MEASURED]`.

**B. The same server under Claude Code 2.1.280.**

- The CLI ran as the bundled `claude.exe -p … --strict-mcp-config --mcp-config mcptap.json` with a
  scratch `CLAUDE_CONFIG_DIR`. `tap.py` logged the wire traffic.
- Claude Code connected on the v2 runtime (`mcp runtime arm: v2`) and negotiated **`2025-11-25`**
  (`protocolEra: legacy`). stdio servers get the 2026-07-28 revision only with
  `MCP_PROTOCOL_NEGOTIATION=auto`.
- It sent `initialize` with `capabilities: {"roots":{"listChanged":true},"elicitation":{}}` and
  `clientInfo {"name":"claude-code","version":"2.1.280"}`.
- It then issued one `tools/list`, and the server answered with all 150 tools. No error or truncation
  appeared in `debug1.log` `[MEASURED]`.
- **The model turn could not run.** A scratch config dir has no login, so the CLI returned "Not logged
  in · Please run /login". Copying the user's OAuth credentials into a scratch dir was ruled out: it
  handles secrets, and a token refresh could invalidate the user's own session.
- The "call `t_137` through the model" step is therefore **not run-verified**. Section C and the docs
  cover it instead.

**C. Reachability at scale, in this very session.**

- This Claude Code session (VS Code panel, entrypoint `claude-vscode`) has about **237 MCP tools from 11
  servers**, including kiwrap's 33, plus 19 deferred built-in tools. All of them are deferred.
- Three `ToolSearch` keyword probes over this catalog `[MEASURED]`:

| Query | Top results (in order) | kiwrap tool wanted | Rank |
|---|---|---|---|
| "check board clearance violations DRC" | `kw_pcb_drc`, `kw_pcb_check`, `kw_sch_check`, `kw_pcb_fix`, `kw_pcb_route_auto` | `kw_pcb_drc` | 1 |
| "search the library for a symbol or footprint" | `WebSearch`, `kw_lib_search`, Gmail `forward`, `kw_sch_add_symbol`, Gmail `search_threads` | `kw_lib_search` | 2 |
| "export gerber and drill files for fabrication" | Claude Docs `export`, `kw_fab_export`, Drive `search_files` | `kw_fab_export` | 2 |

What the probes show:

- **Generic verbs** ("search", "export") pull in other servers' tools.
- **Domain nouns** ("DRC", "board", "footprint", "gerber") put kiwrap first.
- kiwrap's instructions and descriptions should therefore keep saying *KiCad / schematic / board / PCB*.
  Names should stay `kw_<stage>_<verb>`: the spec's advice is "prefix by service or resource so one
  search matches the whole group".

**D. Context cost of a schema.**

- Size of kiwrap's `tools/list`, counting only what reaches the model (name + description + input
  schema; `outputSchema` is not sent to the model):
  - **33 tools = 65,064 chars.** Largest: `kw_pcb_route_auto` 3,906; `kw_sch_add_item` 3,653; `kw_live`
    3,338. Smallest: `kw_help` 449.
  - Output schemas add another 75,016 chars that stay client-side `[MEASURED]`.
- Tokens, from this session's transcript usage: the `ToolSearch` that loaded 5 kiwrap schemas
  (10,832 chars) grew the next request by 6,479 input tokens. Of these, 2,182 were the previous turn's
  own output, which leaves **≈ 4.3k tokens for 5 tools, about 2.5 chars/token** for kiwrap-style JSON
  schemas `[MEASURED]`.
- Projected from that ratio:
  - The **full 33-tool catalog up front costs ≈ 26k tokens per turn**: 13% of a 200k window, or 2.6% of
    1M. `auto` mode would defer it on a 200k window.
  - 150 trivial tools cost ≈ 14k tokens.
- Anthropic's own figure: "58 tools consuming approximately 55K tokens"
  ([Advanced tool use, 2025-11-24](https://www.anthropic.com/engineering/advanced-tool-use)).
- Two cheap schema cuts matter mainly when tool search is off `[MEASURED]`:
  - Stripping pydantic's redundant `title` keys: −10.1%.
  - Also collapsing `anyOf:[X,{"type":"null"}]` to `X`: −15.4% in total.

### 1.4 Does the number of tools affect selection?

The published evidence says yes when tools are loaded **at once**. Tool search exists for that reason.

- "Claude's ability to pick the right tool degrades once you exceed 30–50 available tools"
  ([tool-search docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool)).
  The Agent SDK docs say the same: "Tool selection accuracy degrades with more than 30-50 tools loaded
  at once".
- With tool search, "Opus 4 improved from 49% to 74%, and Opus 4.5 improved from 79.5% to 88.1%" on
  Anthropic's MCP evals. "The most common failures are wrong tool selection and incorrect parameters,
  especially when tools have similar names"
  ([Advanced tool use](https://www.anthropic.com/engineering/advanced-tool-use)).
- With deferral, what matters is how the 3–5 tools returned per search are **ranked** (section 1.3 C),
  not how many tools the catalog holds.
- No accuracy eval was run here (no API credentials). The claim that selection quality depends on count
  rests on the vendor figures above, not on a local measurement.

### 1.5 Recommendation: coherent tools, no generic dispatcher

- **Keep the catalog as domain tools** (`kw_<stage>_<verb>` with `action` / `checks` / `kind` enums).
  Let it grow past 36 when a capability needs its own schema, for example `kw_pcb_analyze` and the
  upgrade tool in section 2.5.
- Use the review's T6 folding rule (fold into an existing tool when the arguments are the same shape)
  as the test for a new tool, not a hard number.
- A soft ceiling of about 45 keeps the up-front cost at or below about 35k tokens for sessions where
  tool search is off.

**Why not a `kw_call(tool, args)` dispatcher.** It would throw away everything Claude Code does per tool:

1. The API-side input-schema validation and the argument guidance the model gets from each schema. The
   vendor's top failure mode is "incorrect parameters".
2. Tool search indexing of names, argument names and argument descriptions. One opaque tool is found
   for everything or for nothing.
3. Per-tool **permission rules**, for example allow `mcp__kiwrap__kw_pcb_get` and prompt on
   `kw_pcb_route`. D23's read-only story depends on these.
4. Per-tool `_meta`: `maxResultSizeChars`, `requiresUserInteraction` (section 2.5) and `alwaysLoad`.
   Also the annotations `readOnlyHint` / `destructiveHint` (review T8).
5. Clear transcripts and hook matchers (`PreToolUse` matcher `mcp__kiwrap__kw_pcb_.*`).

If kiwrap ever has to serve a capped client again (Copilot 128, Cursor 40, both out of scope under D18):

- Add a server flag such as `kiwrap serve --profile compact` that registers a grouped subset.
- Keep the full catalog as the default.
- Never use a free-form dispatcher.

**Worth doing now** (small, all in `core/tools` and `core/server.py`):

- Keep every description at or under about 1,500 chars, with the domain nouns first (the 2,048 cap
  truncates silently).
- Strip `title` keys and collapse nullable `anyOf` in the published input schemas (−15%).
- Keep the server instructions under 2,048 chars (they are 1,195 today) and have them list the tool
  families. The docs say instructions "help Claude understand when to search for your tools".
- Do not set `alwaysLoad`. The session already surfaces kiwrap through the instructions, and each
  always-loaded tool costs about 0.9k tokens on every turn.

## 2. Elicitation (D21 consent prompt)

### 2.1 Claude Code support

| Surface | Form mode | URL mode | What the user sees | Source / evidence |
|---|---|---|---|---|
| CLI in a terminal (incl. VS Code's integrated terminal and the extension's `claudeCode.useTerminal` mode) | yes, since **2.1.76** (2026-03-14) | yes on 2026-07-28 connections since **2.1.281**; the CLI opens the system URL handler, URLs capped at about 8k chars (≈4k when mostly %-escapes) | an interactive dialog rendering the form fields; the dialog blocks the call (no auto-backgrounding while it is open); forms taller than the terminal scroll (2.1.239) | [mcp#respond-to-mcp-elicitation-requests](https://code.claude.com/docs/en/mcp), changelog 2.1.76 / 2.1.239 / 2.1.281 |
| Hooks | `Elicitation` (answer or deny without a dialog; exit code 2 denies), `ElicitationResult` (observe or override the user's answer); the matcher is the MCP server name | same | none when a hook answers | [hooks#elicitation](https://code.claude.com/docs/en/hooks) |
| `claude -p` / Agent SDK (`stream-json`) | forwarded to the SDK host as a control request `subtype:"elicitation"`; the CLI returns `cancel` if the host does not answer elicitations | same | whatever the host renders | 2.1.280 binary: `handleElicitation(){if(!this.hostAnswersElicitations)return{action:"cancel"}…}` `[MEASURED, static]` |
| **VS Code chat panel (default UI), 2.1.280** | **not shown: auto-decline** | not shown | **nothing**; the server receives `{"action":"decline"}` | `extension.js` 2.1.280: `spawnClaude()` passes `canUseTool`, `onUserDialog`, hooks… but **no `onElicitation`**. The SDK's handler is `if(this.onElicitation){…}return{action:"decline"}`. `webview/index.js` has no elicitation strings. `[MEASURED, static]` |

**Consequences for kiwrap:**

- A capability check cannot detect the panel. The CLI child always sends `elicitation: {}`, and
  `clientInfo` is `claude-code` in both surfaces `[MEASURED]`.
- The only observable difference is the environment the MCP server inherits:
  - `CLAUDE_CODE_ENTRYPOINT=claude-vscode` under the panel (this session).
  - `sdk-cli` under `claude -p` `[MEASURED]`.

  This is an undocumented heuristic. Use it only to *skip* a doomed elicitation, never to grant
  anything.
- The docs' CLI-vs-extension table
  ([vs-code#vs-code-extension-vs-claude-code-cli](https://code.claude.com/docs/en/vs-code)) does not
  mention elicitation. Re-check at each extension bump by grepping `extension.js` for `onElicitation:`
  in the `spawnClaude` options. Anthropic may add it at any time.

### 2.2 MCP protocol facts that matter

From the [spec 2025-11-25, client/elicitation](https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation):

- Clients that support elicitation MUST declare `elicitation`. `{}` is equivalent to `{form:{}}`. URL
  mode was added in 2025-11-25.
- Servers MUST NOT send a mode the client did not declare. Clients answer an undeclared mode with
  `-32602`.
- Form schemas are flat objects of primitives only (string / number / integer / boolean / enum,
  single- or multi-select). Form mode MUST NOT be used for secrets.
- The three actions are `accept` (with `content`), `decline` (an explicit no) and `cancel` (dismissed).
- In 2026-07-28 (research/06), server-initiated requests become the **Multi Round-Trip Request**
  pattern. The server returns `InputRequiredResult`, and the client retries the tool call with
  `input_responses` / `request_state`.

### 2.3 Python `mcp` 2.2.0 (kiwrap's SDK)

The API surface in `.venv/Lib/site-packages/mcp`:

- `Context.elicit(message, schema: type[BaseModel])` returns `AcceptedElicitation[T] |
  DeclinedElicitation | CancelledElicitation`. It is form mode, and the schema is checked against the
  spec's primitive subset.
- `Context.elicit_url(message, url, elicitation_id)`, then `ctx.session.send_elicit_complete(id)`.
- `ServerSession.elicit_form` / `elicit_url` at the low level.
- The resolver DI in `mcp.server.mcpserver.resolve`:
  - `Annotated[ElicitationResult[T], Resolve(fn)]`, where `fn` may return a value (no question asked)
    or `Elicit(message, T)`.
  - It runs a standalone `elicitation/create` on ≤ 2025-11-25.
  - It runs `InputRequiredResult` rounds on ≥ 2026-07-28.
  - It checks the client capability first.

Measured with `probe_client.py` and `nocap.py` against `elicit_server.py`. The rows are the API used
and the protocol negotiated; the columns are what the client declared.

| API used, protocol | Client declares elicitation (accept / decline) | Client does not declare it |
|---|---|---|
| `ctx.elicit()`, 2025-11-25 | accept → `{"confirm":true,…}`; decline → `DeclinedElicitation` | request sent anyway: the client answers the JSON-RPC error "Elicitation not supported", which the tool receives as an `MCPError` |
| `ctx.elicit()`, 2026-07-28 | **`NoBackChannelError: Cannot send 'elicitation/create'`**, whatever the client supports | same `NoBackChannelError` |
| `Resolve` + `Elicit`, 2025-11-25 | accept / decline delivered to the tool | JSON-RPC error `-32021` "Client did not declare the form elicitation capability", `data.requiredCapabilities={"elicitation":{"form":{}}}`: a protocol error, **not** an `isError` tool result |
| `Resolve` + `Elicit`, 2026-07-28 | accept / decline delivered (MRTR round trip) | same `-32021` |

Consequences for the code:

- Use the **resolver form** so the code survives Claude Code turning 2026-07-28 on for stdio (today that
  needs `MCP_PROTOCOL_NEGOTIATION=auto`).
- Before asking, check `ctx.session.client_capabilities.elicitation` in the resolver, and return the
  teaching error when it is absent. The SDK's `-32021` is a protocol error, which clients "are less
  likely to recover from" (spec, tools §Error Handling).

### 2.4 The consent problem, stated precisely

D21 needs a **human** yes before kiwrap re-stamps a user's KiCad files to a newer format. In the product
surface (VS Code panel, 2.1.280) the three candidate mechanisms behave as follows.

| Mechanism | Reaches a human in the panel? | Can the model fake it? | Notes |
|---|---|---|---|
| MCP form elicitation | **no** (auto-decline) | no | Works in the CLI / terminal mode. The panel's `decline` looks like a refusal. |
| Teaching error, then `kw_project(action='upgrade', confirm=true)` | only if the agent asks in chat | **yes**: the model can set `confirm=true` itself | Works with every client. It is consent only as far as the agent is honest and the permission mode prompts. |
| Tool with `_meta["anthropic/requiresUserInteraction"]: true` | **yes**: a permission prompt on every call, even in `acceptEdits` / `auto` / `bypassPermissions`, with no "don't ask again"; allow rules do not skip it; `dontAsk` mode denies | **no** | Documented for v2.1.199+ ([mcp#require-approval-for-a-specific-tool](https://code.claude.com/docs/en/mcp)); SDK hosts' `canUseTool` receives it, and the panel passes `canUseTool`. The key is present in the 2.1.280 binary `[MEASURED]`. The panel prompt itself was not run-verified (no credentials). |
| kiwrap extension modal (D21's third leg) | yes, if the kiwrap extension is installed and running | no, if the server checks a record only the extension writes | Needs a channel. The only cross-process channel today is files under the storage home (EXTENSION.md §3: session records). It adds UI code and a race to design. |

### 2.5 Recommendation for D21

1. **Stop and teach (every client).** Any write that would change the project's file-format epoch
   refuses with `E_FORMAT_UPGRADE_REQUIRED`, the teaching-error code the in-progress Phase 0 work uses
   (`extension/src/consent.ts`, uncommitted at the time of writing). It names:
   - the project's epoch and KiCad (for example 10.0 / `20250114`);
   - the target (10.99);
   - the files affected;
   - the two ways forward: install the matching KiCad (link), or upgrade through the dedicated tool.

   Reads keep working on copies or in memory, as D21 already says.
2. **Consent through a dedicated tool with `anthropic/requiresUserInteraction`.**
   - Name it `kw_project_upgrade(project, from_kicad, to_kicad, files[])`, or move `upgrade` out of
     `kw_project`: `_meta` is per tool, not per action.
   - Claude Code's permission prompt, which shows the tool name and its arguments, *is* the consent.
   - The server re-derives `from_kicad`, `to_kicad` and `files` and **refuses on any mismatch**, so the
     arguments the user approves are the truth.
   - The upgrade runs on a snapshot first (existing `mutation()` path).
   - Cost: one catalog slot, which section 1 shows is free.
   - Retire `kw_project(action='upgrade', confirm=true)` as the agent's route. `confirm=true` is the
     model attesting consent to itself.
   - The extension's modal (4) can call the same `kw_project_upgrade` on its own MCP client. The
     `_meta` flag only changes what Claude Code does; the extension already has the human's click.
3. **Elicitation as an enhancement, not the gate.** When the client declares form elicitation and
   `CLAUDE_CODE_ENTRYPOINT` is not `claude-vscode`, the upgrade tool may also ask for a one-field
   boolean form (resolver API, section 2.3).
   - `accept`+`true` proceeds.
   - `decline`, `cancel` or an error all fall back to (1). "decline" is never stored as a lasting
     refusal.
   - Never ask for anything sensitive in the form (spec).
4. **Extension modal: covers only the extension's own calls.**
   - The in-progress `extension/src/consent.ts` turns `E_FORMAT_UPGRADE_REQUIRED` into a modal
     ([Upgrade] [Install KiCad x…] [Cancel]) for calls the **extension's own** kiwrap server makes: ERC
     on save, DRC, `kw_live`.
   - That is a real human gate, and it should stay.
   - It never sees the **agent's** calls. Claude Code spawns its own kiwrap server over stdio, and the
     two servers share only the storage home (EXTENSION.md §3). The agent path therefore needs (2).
   - A later extension could also *notify* about an agent-side refusal by watching a record under
     `<storage>/consent/pending/`. Gating stays in (2) and in the extension's own modal, so no new trust
     channel has to be secured.
5. **E2E check to add (Phase 0).**
   - Run the panel scenario with a real login: an agent tries to save a 10.0 project on 10.99.
   - Expect the refusal, then a permission prompt for `kw_project_upgrade` that shows the from/to
     versions.
   - Deny: the files are byte-identical. Approve: they are upgraded, and a snapshot exists.
   - Also assert that an elicitation sent under the panel comes back as `decline`, so a future extension
     update that starts rendering it is noticed.

## 3. Open items and caveats

- **No end-to-end Claude Code run.** A scratch `CLAUDE_CONFIG_DIR` has no login, so the model-side steps
  were not executed:
  - calling `t_137` among 150 tools;
  - the panel's elicitation decline;
  - the `requiresUserInteraction` prompt.

  The client-side handshake was measured (`tap_*.log`, `debug1.log`), and the bundle was read.
- The token ratio (≈2.5 chars/token) comes from one `ToolSearch` load in this session's transcript. It
  is good to about ±10%, because the previous turn's output was subtracted.
- The panel's missing `onElicitation` is version-specific (2.1.280). The CLI changelog is active here:
  URL mode arrived in 2.1.281, dated the day before this note.

## Sources

- Claude Code MCP docs (tool search, limits, elicitation, `requiresUserInteraction`, `alwaysLoad`):
  <https://code.claude.com/docs/en/mcp> (fetched 2026-09-24)
- Agent SDK tool search: <https://code.claude.com/docs/en/agent-sdk/tool-search>
- Hooks (`Elicitation`, `ElicitationResult`): <https://code.claude.com/docs/en/hooks>
- VS Code extension docs: <https://code.claude.com/docs/en/vs-code>
- Changelog (2.1.7, 2.1.9, 2.1.76, 2.1.113, 2.1.117, 2.1.239, 2.1.280, 2.1.281):
  <https://code.claude.com/docs/en/changelog>
- Claude API tool search tool: <https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool>
- Anthropic engineering, "Advanced tool use" (2025-11-24): <https://www.anthropic.com/engineering/advanced-tool-use>
- MCP spec 2025-11-25, tools: <https://modelcontextprotocol.io/specification/2025-11-25/server/tools>
- MCP spec 2025-11-25, elicitation: <https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation>
- VS Code Copilot tools (128 limit): <https://code.visualstudio.com/docs/copilot/agents/agent-tools>
- Cursor MCP docs: <https://cursor.com/docs/context/mcp>
- Cursor 40-tool reports: <https://forum.cursor.com/t/mcp-server-40-tool-limit-in-cursor-is-this-frustrating-your-workflow/81627>
- Local:
  - `anthropic.claude-code-2.1.280-win32-x64/extension.js` and `resources/native-binary/claude.exe`
  - `.venv/Lib/site-packages/mcp/server/{elicitation.py,mcpserver/context.py,mcpserver/resolve.py,session.py}` (mcp 2.2.0)
