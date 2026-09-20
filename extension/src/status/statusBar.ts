/** Status bar item: server state + ERC / DRC counts; click opens the Problems panel. */
import * as vscode from 'vscode';
import type { DiagnosticsManager } from '../diagnostics/manager';
import type { KiwrapSession } from '../session';

export class KiwrapStatusBar implements vscode.Disposable {
  private readonly item: vscode.StatusBarItem;
  private readonly sub: vscode.Disposable;

  constructor(
    private readonly session: KiwrapSession,
    private readonly diagnostics: DiagnosticsManager,
  ) {
    this.item = vscode.window.createStatusBarItem('kiwrap.status', vscode.StatusBarAlignment.Left, 50);
    this.item.name = 'KiCad (kiwrap)';
    this.item.command = 'kiwrap.showProblems';
    this.sub = session.onDidChange(() => this.update());
    this.update();
    this.item.show();
  }

  update(): void {
    const s = this.session;
    const busy = s.busy.size ? '$(sync~spin) ' : '';
    const state =
      s.serverState === 'ready'
        ? ''
        : s.serverState === 'starting'
          ? ' starting'
          : s.serverState === 'failed'
            ? ' $(warning) server'
            : ' stopped';
    const parts: string[] = [];
    if (s.lastErc) {
      parts.push(`ERC ${s.lastErc.numbers.errors}/${s.lastErc.numbers.warnings}`);
    }
    if (s.lastDrc) {
      parts.push(`DRC ${s.lastDrc.numbers.must_fix}/${s.lastDrc.numbers.review}/${s.lastDrc.numbers.cosmetic}`);
    }
    this.item.text = `${busy}$(circuit-board) kiwrap${state}${parts.length ? ' ' + parts.join(' ') : ''}`;
    const c = this.diagnostics.counts();
    const lines = [
      `kiwrap server: ${s.serverState}${s.server?.pid ? ` (pid ${s.server.pid})` : ''}`,
      s.project ? `project: ${s.project.name}${s.projectStatus ? ` - stage ${s.projectStatus.stage}` : ''}` : 'no project',
      s.lastErc
        ? `ERC: ${s.lastErc.ok ? 'ok' : 'FAIL'} errors=${s.lastErc.numbers.errors} warnings=${s.lastErc.numbers.warnings} (${c.erc.errors}/${c.erc.warnings}/${c.erc.infos} in Problems)`
        : 'ERC: not run',
      s.lastDrc
        ? `DRC: ${s.lastDrc.ok ? 'ok' : 'FAIL'} must_fix=${s.lastDrc.numbers.must_fix} review=${s.lastDrc.numbers.review} cosmetic=${s.lastDrc.numbers.cosmetic} unconnected=${s.lastDrc.numbers.unconnected}`
        : 'DRC: not run',
      'Click: Problems panel',
    ];
    this.item.tooltip = lines.join('\n');
    const failing = (s.lastErc && !s.lastErc.ok) || (s.lastDrc && !s.lastDrc.ok) || s.serverState === 'failed';
    this.item.backgroundColor = failing ? new vscode.ThemeColor('statusBarItem.warningBackground') : undefined;
  }

  dispose(): void {
    this.sub.dispose();
    this.item.dispose();
  }
}
