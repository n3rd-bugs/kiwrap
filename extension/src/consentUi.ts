/**
 * The VS Code side of consent.ts: the modal (with an explicit Cancel as the close affordance), the
 * non-modal messages and the "Install KiCad <x>..." action. The install hint is typed into a
 * terminal but not run: installing software is the user's call (they press Enter), and the
 * message says to refresh the installs afterwards (kw_env refresh).
 */
import * as vscode from 'vscode';
import { CANCEL_BUTTON, KICAD_DOWNLOAD_URL, type ConsentUi } from './consent';

export function vscodeConsentUi(log: (line: string) => void): ConsentUi {
  return {
    async modal(message, detail, buttons) {
      const items: vscode.MessageItem[] = buttons.map((title) => ({ title, isCloseAffordance: title === CANCEL_BUTTON }));
      const pick = await vscode.window.showWarningMessage(message, { modal: true, detail }, ...items);
      return pick && pick.title !== CANCEL_BUTTON ? pick.title : undefined;
    },
    async error(message, buttons = []) {
      return vscode.window.showErrorMessage(message, ...buttons);
    },
    async info(message, buttons = []) {
      return vscode.window.showInformationMessage(message, ...buttons);
    },
    async install(hint, version) {
      const what = version ? `KiCad ${version}` : 'KiCad';
      if (hint) {
        const terminal = vscode.window.createTerminal({ name: `Install ${what}` });
        terminal.show();
        // typed, not executed: the user reviews the command and presses Enter
        terminal.sendText(hint, false);
        log(`install hint in the terminal "Install ${what}": ${hint}`);
        void vscode.window
          .showInformationMessage(
            `kiwrap: press Enter in the "Install ${what}" terminal to run: ${hint}. Then refresh the KiCad installs.`,
            'Refresh installs',
          )
          .then((c) => (c ? vscode.commands.executeCommand('kiwrap.refresh') : undefined));
        return;
      }
      log(`opening ${KICAD_DOWNLOAD_URL} for ${what}`);
      await vscode.env.openExternal(vscode.Uri.parse(KICAD_DOWNLOAD_URL));
    },
    log,
  };
}
