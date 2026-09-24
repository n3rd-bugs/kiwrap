/**
 * The "KiCad (kiwrap)" view: server, installs (kw_env; the selected / project / newest rows
 * marked), engines, the current project (stage, the effective target KiCad and its source, the file
 * format, last validator numbers) and the live session state.
 *
 * An install row's click only expands its details: selecting an install is the inline "Select"
 * action, the context menu or the picker (a one-click selection used to rewrite kiwrap.kicadInstall
 * by accident). The project's target follows D21 (installModel.ts).
 */
import * as path from 'node:path';
import * as vscode from 'vscode';
import type { KiwrapSession, ValidatorSummary } from '../session';
import type { InstallRow } from '../types';
import {
  effectiveTarget,
  formatField,
  installDetails,
  installLabel,
  installRoles,
  installSpec,
  rolesText,
  targetField,
} from './installModel';

export { installLabel, installSpec, matchesSpec } from './installModel';

export type NodeKind =
  | 'server'
  | 'installs'
  | 'install'
  | 'engines'
  | 'engine'
  | 'project'
  | 'projectField'
  | 'erc'
  | 'drc'
  | 'validatorField'
  | 'live'
  | 'liveField'
  | 'installField'
  | 'projectWarning'
  | 'info';

export class KiwrapNode extends vscode.TreeItem {
  constructor(
    public readonly kind: NodeKind,
    label: string,
    public readonly children: KiwrapNode[] = [],
    opts: {
      description?: string;
      tooltip?: string;
      icon?: string;
      contextValue?: string;
      command?: vscode.Command;
      collapsed?: boolean;
      /** For install nodes: the version spec kw_env lists. */
      value?: string;
    } = {},
  ) {
    super(
      label,
      children.length
        ? opts.collapsed
          ? vscode.TreeItemCollapsibleState.Collapsed
          : vscode.TreeItemCollapsibleState.Expanded
        : vscode.TreeItemCollapsibleState.None,
    );
    this.description = opts.description;
    this.tooltip = opts.tooltip;
    if (opts.icon) {
      this.iconPath = new vscode.ThemeIcon(opts.icon);
    }
    this.contextValue = opts.contextValue ?? kind;
    this.command = opts.command;
    this.value = opts.value;
  }
  value: string | undefined;
}

function capBadges(row: InstallRow): string {
  const caps = row.capabilities ?? {};
  const b: string[] = [];
  if (caps['ipc.headless']) {
    b.push('ipc11');
  }
  if (caps['cli.has_drc']) {
    b.push('drc');
  }
  if (caps['cli.has_erc']) {
    b.push('erc');
  }
  if (caps['cli.has_render']) {
    b.push('render');
  }
  const patches = caps['kiwrap.patches'];
  if (Array.isArray(patches) && patches.length) {
    b.push(`patches: ${patches.join(',')}`);
  }
  return b.join(' ');
}

function validatorNode(kind: 'erc' | 'drc', label: string, v: ValidatorSummary | undefined, cmd: string): KiwrapNode {
  if (!v) {
    return new KiwrapNode(kind, label, [], {
      description: 'not run',
      icon: 'circle-outline',
      command: { command: cmd, title: label },
    });
  }
  const fields = Object.entries(v.numbers).map(
    ([k, n]) => new KiwrapNode('validatorField', k, [], { description: String(n) }),
  );
  fields.push(
    new KiwrapNode('validatorField', 'provider', [], { description: v.provider }),
    new KiwrapNode('validatorField', 'at', [], { description: v.at.toLocaleTimeString() }),
  );
  return new KiwrapNode(kind, label, fields, {
    description: `${v.ok ? 'ok' : 'FAIL'} ${Object.entries(v.numbers)
      .map(([k, n]) => `${k}=${n}`)
      .join(' ')}`,
    icon: v.ok ? 'pass' : 'error',
    collapsed: true,
  });
}

export class KiwrapTree implements vscode.TreeDataProvider<KiwrapNode>, vscode.Disposable {
  private readonly changed = new vscode.EventEmitter<KiwrapNode | undefined>();
  readonly onDidChangeTreeData = this.changed.event;
  private readonly sub: vscode.Disposable;

  constructor(private readonly session: KiwrapSession) {
    this.sub = session.onDidChange(() => this.changed.fire(undefined));
  }

  refresh(): void {
    this.changed.fire(undefined);
  }

  getTreeItem(el: KiwrapNode): vscode.TreeItem {
    return el;
  }

  getChildren(el?: KiwrapNode): KiwrapNode[] {
    if (el) {
      return el.children;
    }
    return this.roots();
  }

  /** The top-level nodes (exported for the integration test). */
  roots(): KiwrapNode[] {
    const s = this.session;
    return [this.serverNode(), this.installsNode(), this.enginesNode(), this.projectNode(), this.liveNode()].filter(
      (n): n is KiwrapNode => !!n && !!s,
    );
  }

  private serverNode(): KiwrapNode {
    const s = this.session;
    const spec = s.server?.client.serverSpec;
    const desc =
      s.serverState === 'ready'
        ? `running${s.server?.pid ? ` (pid ${s.server.pid})` : ''}`
        : s.serverState === 'failed'
          ? `failed: ${s.serverDetail}`
          : s.serverState;
    const icon =
      s.serverState === 'ready' ? 'vm-running' : s.serverState === 'starting' ? 'sync~spin' : s.serverState === 'failed' ? 'error' : 'vm';
    return new KiwrapNode('server', 'Server', [], {
      description: s.server ? desc : s.serverError ?? 'not configured',
      tooltip: spec ? `${spec.command} ${spec.args.join(' ')}\ncwd ${spec.cwd}` : s.serverError,
      icon,
      command: { command: 'kiwrap.showOutput', title: 'Show output' },
    });
  }

  private installsNode(): KiwrapNode {
    const s = this.session;
    const rows = s.env?.installs ?? [];
    const explicit = s.explicitInstall();
    const roles = installRoles(rows, {
      explicit,
      effective: effectiveTarget(s.projectStatus) ?? s.projectStatus?.target_kicad ?? null,
      newest: s.env?.default_install ?? null,
    });
    // the row the extension's own launches use: the explicit choice, else the project's target
    const using = rows.find((r) => (explicit ? roles.get(r)?.selected : roles.get(r)?.project));
    const children = rows.map((row) => {
      const spec = installSpec(row, rows);
      const r = roles.get(row);
      const details = installDetails(row).map(
        ([k, v]) => new KiwrapNode('installField', k, [], { description: v, tooltip: `${k}: ${v}` }),
      );
      return new KiwrapNode('install', installLabel(row, rows), details, {
        description: [rolesText(r), capBadges(row)].filter(Boolean).join(' '),
        tooltip:
          `${row.root}\n${row.kicad_cli}\nsource: ${row.source}` +
          `${row.capabilities_error ? `\ncapabilities: ${row.capabilities_error.message}` : ''}` +
          '\n\nClick shows the details; "Select" (inline or right-click) makes kiwrap.kicadInstall use this install.',
        icon: row === using ? 'star-full' : 'circuit-board',
        contextValue: 'install',
        value: spec,
        collapsed: true,
        // no command: a click expands the details instead of changing kiwrap.kicadInstall
      });
    });
    const following = explicit ? `using ${explicit}` : "matching the project's format";
    return new KiwrapNode('installs', 'KiCad installs', children, {
      description: s.envError ?? (rows.length ? `${rows.length} - ${following}` : s.env ? 'none' : ''),
      tooltip: explicit
        ? `kiwrap.kicadInstall = ${explicit}. Select "match the project's file format" to let kiwrap pick (D21).`
        : "kiwrap.kicadInstall is empty: kiwrap uses the KiCad that matches the project's file format (D21).",
      icon: 'library',
      collapsed: false,
    });
  }

  private enginesNode(): KiwrapNode {
    const engines = this.session.env?.engines ?? {};
    const children: KiwrapNode[] = [];
    for (const [name, v] of Object.entries(engines)) {
      if (name === 'autorouter' && v && typeof v === 'object') {
        const a = v as Record<string, unknown>;
        children.push(
          new KiwrapNode('engine', 'autorouter', [], {
            description: a.available ? `ready${a.freerouting_version ? ` (Freerouting ${a.freerouting_version})` : ''}` : `missing${a.reason ? `: ${a.reason}` : ''}`,
            icon: a.available ? 'pass' : 'circle-slash',
            tooltip: JSON.stringify(a, null, 1),
          }),
        );
      } else {
        children.push(
          new KiwrapNode('engine', name, [], { description: v ? 'present' : 'missing', icon: v ? 'pass' : 'circle-slash' }),
        );
      }
    }
    return new KiwrapNode('engines', 'Engines', children, { icon: 'tools', collapsed: true });
  }

  private projectNode(): KiwrapNode {
    const s = this.session;
    const p = s.pickWorkspaceProject();
    if (!p) {
      return new KiwrapNode('project', 'Project', [], { description: 'no .kicad_pro in the workspace', icon: 'folder' });
    }
    const st = s.projectStatus;
    const fields: KiwrapNode[] = [
      new KiwrapNode('projectField', 'path', [], { description: path.relative(vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? p.dir, p.pro) || p.pro, tooltip: p.pro }),
      new KiwrapNode('projectField', 'stage', [], { description: st?.stage ?? '?', icon: 'milestone' }),
    ];
    const target = targetField(st, s.explicitInstall());
    fields.push(
      new KiwrapNode('projectField', 'target KiCad', [], {
        description: target.description,
        tooltip: target.tooltip,
        icon: target.icon,
        contextValue: 'projectTarget',
      }),
    );
    const format = formatField(st, s.env);
    if (format) {
      fields.push(
        new KiwrapNode('projectField', 'file format', [], {
          description: format.description,
          tooltip: format.tooltip,
          icon: format.icon,
          // a format no installed KiCad writes offers the (consented) upgrade inline
          contextValue: format.icon === 'warning' ? 'projectFormatMismatch' : 'projectFormat',
        }),
      );
    }
    const explicit = s.explicitInstall();
    if (explicit && explicit !== effectiveTarget(st)) {
      fields.push(
        new KiwrapNode('projectField', 'live KiCad', [], {
          description: `${explicit} (kiwrap.kicadInstall)`,
          tooltip: 'The extension launches this install for the live view; clear kiwrap.kicadInstall to follow the project.',
        }),
      );
    }
    for (const w of st?.warnings ?? []) {
      fields.push(new KiwrapNode('projectWarning', 'warning', [], { description: w, tooltip: w, icon: 'warning' }));
    }
    fields.push(new KiwrapNode('projectField', 'next', [], { description: st?.next_suggested ?? '' }));
    if (st?.external_edit) {
      fields.push(new KiwrapNode('projectField', 'external edit', [], { description: st.external_edit.files.join(', '), icon: 'warning' }));
    }
    if (st?.editor_locks?.length) {
      fields.push(new KiwrapNode('projectField', 'editor locks', [], { description: st.editor_locks.join(', '), icon: 'lock' }));
    }
    fields.push(validatorNode('erc', 'ERC', s.lastErc, 'kiwrap.runErc'));
    fields.push(validatorNode('drc', 'DRC', s.lastDrc, 'kiwrap.runDrc'));
    return new KiwrapNode('project', p.name, fields, {
      description: st ? `stage ${st.stage}` : '',
      icon: 'folder-opened',
      tooltip: p.pro,
    });
  }

  private liveNode(): KiwrapNode {
    const s = this.session;
    const live = s.live;
    let description: string;
    let icon: string;
    const fields: KiwrapNode[] = [];
    switch (live.kind) {
      case 'unavailable':
        description = `unavailable: ${live.reason}`;
        icon = 'circle-slash';
        break;
      case 'idle':
        // the kw_live text only once the server answered tools/list; before that it is the
        // server's state that matters (reviewer finding 19)
        description =
          s.serverState === 'ready'
            ? s.server?.liveAvailable
              ? 'not running'
              : 'kw_live not published by this server'
            : s.serverState === 'starting'
              ? 'server starting'
              : s.serverState === 'failed'
                ? 'server failed (see Server)'
                : 'server not running';
        icon = 'debug-disconnect';
        break;
      case 'launching':
        description = 'launching';
        icon = 'sync~spin';
        break;
      case 'running':
        description = `pid ${live.session.pid} - KiCad ${live.session.install}`;
        icon = 'debug-start';
        fields.push(
          new KiwrapNode('liveField', 'session', [], { description: live.session.session_id }),
          new KiwrapNode('liveField', 'editors', [], { description: (live.session.editors ?? []).join(', ') }),
          new KiwrapNode('liveField', 'api socket', [], { description: live.session.api_socket ?? '' }),
        );
        break;
      case 'error':
        description = live.message;
        icon = 'error';
        break;
    }
    return new KiwrapNode('live', 'Live KiCad', fields, {
      description,
      icon,
      collapsed: true,
      command: live.kind === 'running' ? undefined : { command: 'kiwrap.openLive', title: 'Open Live KiCad' },
    });
  }

  dispose(): void {
    this.sub.dispose();
    this.changed.dispose();
  }
}
