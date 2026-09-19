// Regenerates contributes.keybindings in package.json: every chord that VS Code binds by default (Windows) and
// that a KiCad user might press while the KiCad view is focused gets rebound to kiwrapShell.forwardKey, guarded
// by the kiwrapShell.rescueKeys context key so the driver can measure both behaviours.
// Run: node tools/gen_keybindings.js
const fs = require('fs');
const path = require('path');
const pkgPath = path.join(__dirname, '..', 'package.json');
const p = JSON.parse(fs.readFileSync(pkgPath, 'utf8'));
const when = "kiwrapShell.rescueKeys && (activeWebviewPanelId == 'kiwrapShell.view' || activeCustomEditorId == 'kiwrapShell.boardEditor')";
const chords = [
  // editor / window management
  'ctrl+w', 'ctrl+f4', 'ctrl+shift+w', 'ctrl+k ctrl+w', 'ctrl+k w', 'ctrl+shift+t', 'ctrl+n', 'ctrl+shift+n', 'ctrl+o', 'ctrl+k ctrl+o', 'ctrl+r', 'ctrl+shift+r',
  'ctrl+tab', 'ctrl+shift+tab', 'ctrl+pageup', 'ctrl+pagedown', 'ctrl+1', 'ctrl+2', 'ctrl+3', 'ctrl+4', 'ctrl+5', 'ctrl+6', 'ctrl+7', 'ctrl+8', 'ctrl+9', 'ctrl+0',
  'ctrl+\\', 'ctrl+k ctrl+\\', 'ctrl+k left', 'ctrl+k right', 'ctrl+k up', 'ctrl+k down', 'ctrl+k enter', 'ctrl+alt+right', 'ctrl+alt+left', 'ctrl+alt+up', 'ctrl+alt+down',
  'shift+alt+0', 'shift+alt+1', 'ctrl+k m', 'ctrl+k z', 'ctrl+k ctrl+t', 'ctrl+k ctrl+s', 'ctrl+k ctrl+x', 'ctrl+k ctrl+q', 'ctrl+k ctrl+f', 'ctrl+k ctrl+c', 'ctrl+k ctrl+u',
  'ctrl+k ctrl+i', 'ctrl+k ctrl+r', 'ctrl+k ctrl+p', 'ctrl+k ctrl+d', 'ctrl+k ctrl+h', 'ctrl+k ctrl+m', 'ctrl+k ctrl+b', 'ctrl+k ctrl+k', 'ctrl+k ctrl+l', 'ctrl+k ctrl+n',
  'ctrl+k ctrl+e', 'ctrl+k ctrl+a', 'ctrl+k ctrl+y', 'ctrl+k ctrl+z', 'ctrl+k ctrl+g', 'ctrl+k ctrl+j', 'ctrl+k ctrl+v', 'ctrl+k ctrl+0', 'ctrl+k ctrl+1', 'ctrl+k ctrl+2',
  'ctrl+k ctrl+3', 'ctrl+k ctrl+4', 'ctrl+k ctrl+5', 'ctrl+k ctrl+6', 'ctrl+k ctrl+7', 'ctrl+k ctrl+8', 'ctrl+k ctrl+9', 'ctrl+k ctrl+shift+s', 'ctrl+k ctrl+shift+w', 'ctrl+k ctrl+shift+o',
  'ctrl+k ctrl+shift+n', 'ctrl+k ctrl+shift+i', 'ctrl+k ctrl+shift+f', 'ctrl+k ctrl+shift+e', 'ctrl+k ctrl+shift+c', 'ctrl+k ctrl+shift+u', 'ctrl+k ctrl+shift+q', 'ctrl+k ctrl+shift+m', 'ctrl+k ctrl+shift+r',
  // palette / quick open / views
  'ctrl+p', 'ctrl+shift+p', 'f1', 'ctrl+e', 'ctrl+q', 'ctrl+b', 'ctrl+j', 'ctrl+shift+e', 'ctrl+shift+f', 'ctrl+shift+g', 'ctrl+shift+x', 'ctrl+shift+m', 'ctrl+shift+d', 'ctrl+shift+h', 'ctrl+shift+a',
  'ctrl+shift+u', 'ctrl+shift+y', 'ctrl+shift+b', 'ctrl+shift+o', 'ctrl+shift+k', 'ctrl+shift+l', 'ctrl+shift+s', 'ctrl+shift+i', 'ctrl+shift+c', 'ctrl+shift+v', 'ctrl+shift+z', 'ctrl+shift+[', 'ctrl+shift+]',
  'ctrl+`', 'ctrl+shift+`', 'ctrl+,', 'ctrl+g', 'ctrl+d', 'ctrl+u', 'ctrl+l', 'ctrl+m', 'ctrl+h', 'ctrl+t', 'ctrl+i', 'ctrl+[', 'ctrl+]', 'ctrl+space', 'ctrl+shift+space', 'ctrl+alt+i', 'ctrl+alt+b', 'ctrl+alt+p', 'ctrl+shift+alt+p',
  // zoom / fullscreen
  'ctrl+=', 'ctrl+-', 'ctrl+shift+=', 'ctrl+shift+-', 'ctrl+numpad_add', 'ctrl+numpad_subtract', 'ctrl+numpad0', 'f11',
  // function keys (dev host would start a debug session on F5)
  'f2', 'f3', 'f4', 'f5', 'f6', 'f7', 'f8', 'f9', 'f10', 'f12', 'ctrl+f5', 'shift+f5', 'ctrl+shift+f5', 'shift+f11', 'ctrl+f11', 'shift+f12', 'ctrl+f12', 'ctrl+shift+f12', 'ctrl+f2', 'shift+f2', 'ctrl+f3', 'shift+f3', 'ctrl+f8', 'shift+f8', 'ctrl+shift+f8', 'ctrl+f9', 'ctrl+f10', 'ctrl+shift+f10', 'shift+f10', 'ctrl+f7', 'shift+f7', 'ctrl+f6', 'shift+f6', 'ctrl+f1', 'shift+f1', 'ctrl+shift+f1',
];
p.contributes.keybindings = chords.map((key) => ({ command: 'kiwrapShell.forwardKey', args: { key }, key, when }));
fs.writeFileSync(pkgPath, JSON.stringify(p, null, 2) + '\n');
console.log('keybindings:', p.contributes.keybindings.length);
