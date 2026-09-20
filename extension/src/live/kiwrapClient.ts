/*
 * How the live module reaches `kw_live` on the kiwrap MCP server:
 *   1. an object ext-core passes to activateLive(context, { client })            (preferred)
 *   2. the command `kiwrap.core.callTool(name, args)` registered by ext-core   (loose coupling)
 *   3. nothing: every kw_live call fails with a clear error; attach-by-pid still works.
 * The pure parts (result unwrapping, KwLive wrapper) live in kwLive.ts.
 */
import * as vscode from 'vscode';
import { CORE_CALL_TOOL_COMMAND, type KiwrapClient } from '../shared/types';

export async function resolveClient(injected?: KiwrapClient): Promise<KiwrapClient | null> {
  if (injected) { return injected; }
  const cmds = await vscode.commands.getCommands(true);
  if (cmds.includes(CORE_CALL_TOOL_COMMAND)) {
    return { callTool: (name, args) => vscode.commands.executeCommand(CORE_CALL_TOOL_COMMAND, name, args) as Promise<unknown> };
  }
  return null;
}

export { KwLive, asSession, sessionSupportsEvents, unwrapToolResult } from './kwLive';
