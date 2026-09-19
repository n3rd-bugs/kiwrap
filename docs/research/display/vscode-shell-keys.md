# VS Code shell: per-shortcut results (measured 2026-09-19, VS Code 1.138.0, Windows 11, 150 % scaling)

Driver: `prototypes/display/vscode-shell/tools/drive_probe.ps1` (Win32 `SendInput` into a real Extension Development Host, isolated profile). Raw logs: scratchpad `display/vscode-shell/run{A,B,C}/probe.jsonl`.

Columns: **A** = webview page focused, our rescue keybindings on; **B** = same page, rescue keybindings off (stock VS Code); **C** = cross-origin `<iframe>` (http://localhost) focused inside the webview.
`yes` = the page received the DOM `keydown`; `[dp]` = VS Code's webview pre-script had already called `preventDefault()` (browser default suppressed, listener still fires); `rescued` = our `contributes.keybindings` entry fired in the extension host and swallowed the VS Code command; `acted` = focus left the page before our recovery keystroke, i.e. VS Code (or the OS) opened something (palette, menu bar, view, dialog); `CLOSED` = the panel was disposed.

| chord | A: page saw | A: rescued | A: VS Code acted | B: page saw | B: VS Code acted | C: iframe saw | C: VS Code acted |
|---|---|---|---|---|---|---|---|
| `r` | yes |  |  | yes |  | yes |  |
| `m` | yes |  |  | yes |  | yes |  |
| `x` | yes |  |  | yes |  | yes |  |
| `v` | yes |  |  | yes |  | yes |  |
| `e` | yes |  |  | yes |  | yes |  |
| `d` | yes |  |  | yes |  | yes |  |
| `f` | yes |  |  | yes |  | yes |  |
| `b` | yes |  |  | yes |  | yes |  |
| `g` | yes |  |  | yes |  | yes |  |
| `a` | yes |  |  | yes |  | yes |  |
| `w` | yes |  |  | yes |  | yes |  |
| `l` | yes |  |  | yes |  | yes |  |
| `p` | yes |  |  | yes |  | yes |  |
| `u` | yes |  |  | yes |  | yes |  |
| `i` | yes |  |  | yes |  | yes |  |
| `n` | yes |  |  | yes |  | yes |  |
| `q` | yes |  |  | yes |  | yes |  |
| `t` | yes |  |  | yes |  | yes |  |
| `h` | yes |  |  | yes |  | yes |  |
| `c` | yes |  |  | yes |  | yes |  |
| `z` | yes |  |  | yes |  | yes |  |
| `s` | yes |  |  | yes |  | yes |  |
| `o` | yes |  |  | yes |  | yes |  |
| `y` | yes |  |  | yes |  | yes |  |
| `k` | yes |  |  | yes |  | yes |  |
| `j` | yes |  |  | yes |  | yes |  |
| `shift+r` | yes |  |  | yes |  | yes |  |
| `shift+m` | yes |  |  | yes |  | yes |  |
| `shift+f` | yes |  |  | yes |  | yes |  |
| `shift+g` | yes |  |  | yes |  | yes |  |
| `shift+n` | yes |  |  | yes |  | yes |  |
| `shift+a` | yes |  |  | yes |  | yes |  |
| `shift+space` | yes |  |  | yes |  | yes |  |
| `shift+delete` | yes |  |  | yes |  | yes |  |
| `delete` | yes |  |  | yes |  | yes |  |
| `backspace` | yes |  |  | yes |  | yes |  |
| `esc` | yes |  |  | yes |  | yes |  |
| `tab` | yes |  |  | yes |  | yes |  |
| `shift+tab` | yes |  |  | yes |  | yes |  |
| `space` | yes |  |  | yes |  | yes |  |
| `enter` | yes |  |  | yes |  | yes |  |
| `insert` | yes |  |  | yes |  | yes |  |
| `home` | yes |  |  | yes |  | yes |  |
| `end` | yes |  |  | yes |  | yes |  |
| `pageup` | yes |  |  | yes |  | yes |  |
| `pagedown` | yes |  |  | yes |  | yes |  |
| `left` | yes |  |  | yes |  | yes |  |
| `right` | yes |  |  | yes |  | yes |  |
| `up` | yes |  |  | yes |  | yes |  |
| `down` | yes |  |  | yes |  | yes |  |
| `shift+left` | yes |  |  | yes |  | yes |  |
| `ctrl+left` | yes |  |  | yes |  | yes |  |
| `ctrl+shift+right` | yes |  | acted | yes |  | yes |  |
| `1` | yes |  |  | yes |  | yes |  |
| `2` | yes |  |  | yes |  | yes |  |
| `3` | yes |  |  | yes |  | yes |  |
| `4` | yes |  |  | yes |  | yes |  |
| `5` | yes |  |  | yes |  | yes |  |
| `6` | yes |  |  | yes |  | yes |  |
| `7` | yes |  |  | yes |  | yes |  |
| `8` | yes |  |  | yes |  | yes |  |
| `9` | yes |  |  | yes |  | yes |  |
| `0` | yes |  |  | yes |  | yes |  |
| `-` | yes |  |  | yes |  | yes |  |
| `=` | yes |  |  | yes |  | yes |  |
| `[` | yes |  |  | yes |  | yes |  |
| `]` | yes |  |  | yes |  | yes |  |
| `\` | yes |  |  | yes |  | yes |  |
| `;` | yes |  |  | yes |  | yes |  |
| `,` | yes |  |  | yes |  | yes |  |
| `.` | yes |  |  | yes |  | yes |  |
| `/` | yes |  |  | yes |  | yes |  |
| ``` | yes |  |  | yes |  | yes |  |
| `numpad_add` | yes |  |  | yes |  | yes |  |
| `numpad_subtract` | yes |  |  | yes |  | yes |  |
| `numpad0` | yes |  |  | yes |  | yes |  |
| `numpad1` | yes |  |  | yes |  | yes |  |
| `numpad_multiply` | yes |  |  | yes |  | yes |  |
| `numpad_divide` | yes |  |  | yes |  | yes |  |
| `numpad_decimal` | yes |  |  | yes |  | yes |  |
| `ctrl+z` | yes [dp] |  |  | yes [dp] |  | yes |  |
| `ctrl+y` | yes [dp] |  |  | yes [dp] |  | yes |  |
| `ctrl+shift+z` | yes [dp] | yes |  | yes [dp] |  | yes |  |
| `ctrl+x` | yes [dp] |  |  | yes [dp] |  | yes |  |
| `ctrl+c` | yes [dp] |  |  | yes [dp] |  | yes |  |
| `ctrl+v` | yes [dp] |  |  | yes [dp] |  | yes |  |
| `ctrl+s` | yes [dp] |  |  | yes [dp] |  | yes |  |
| `ctrl+a` | yes |  |  | yes |  | yes |  |
| `ctrl+f` | yes [dp] |  |  | yes [dp] |  | yes |  |
| `ctrl+shift+s` | yes [dp] | yes |  | yes [dp] | acted | yes |  |
| `ctrl+shift+a` | yes | yes |  | yes | acted | yes |  |
| `ctrl+d` | yes | yes |  | yes |  | yes |  |
| `ctrl+e` | yes | yes |  | yes | acted | yes |  |
| `ctrl+h` | yes | yes |  | yes |  | yes |  |
| `ctrl+i` | yes | yes |  | yes |  | yes |  |
| `ctrl+l` | yes | yes |  | yes |  | yes |  |
| `ctrl+m` | yes | yes |  | yes |  | yes |  |
| `ctrl+r` | yes | yes |  | yes |  | yes |  |
| `ctrl+t` | yes | yes |  | yes |  | yes |  |
| `ctrl+u` | yes | yes |  | yes |  | yes |  |
| `ctrl+g` | yes | yes |  | yes | acted | yes |  |
| `ctrl+k` | yes |  |  | yes |  | yes |  |
| `ctrl+b` | yes | yes |  | yes | acted | yes |  |
| `ctrl+j` | yes | yes |  | yes | acted | yes |  |
| `ctrl+shift+f` | yes [dp] | yes |  | yes [dp] | acted | yes |  |
| `ctrl+shift+m` | yes | yes |  | yes | acted | yes |  |
| `ctrl+shift+v` | yes [dp] | yes |  | yes [dp] |  | yes |  |
| `ctrl+shift+c` | yes [dp] | yes |  | yes [dp] |  | yes |  |
| `ctrl+shift+x` | yes [dp] | yes |  | yes [dp] | acted | yes |  |
| `ctrl+shift+e` | yes | yes |  | yes | acted | yes |  |
| `ctrl+shift+g` | yes | yes |  | yes | acted | yes |  |
| `ctrl+shift+d` | yes | yes |  | yes | acted | yes |  |
| `ctrl+shift+h` | yes | yes |  | yes | acted | yes |  |
| `ctrl+shift+i` | yes | yes |  | yes | acted | yes |  |
| `ctrl+shift+u` | yes | yes |  | yes | acted | yes |  |
| `ctrl+shift+b` | yes | yes |  | yes |  | yes |  |
| `ctrl+shift+r` | yes | yes |  | yes |  | yes |  |
| `ctrl+shift+o` | yes | yes |  | yes | acted | yes |  |
| `ctrl+shift+t` | yes | yes |  | yes |  | yes |  |
| `ctrl+shift+l` | yes | yes |  | yes |  | yes |  |
| `ctrl+shift+k` | yes | yes |  | yes |  | yes |  |
| `ctrl+shift+y` | yes [dp] | yes |  | yes [dp] | acted | yes |  |
| `ctrl+1` | yes | yes |  | yes |  | yes |  |
| `ctrl+2` | yes | yes |  | yes | acted | yes |  |
| `ctrl+3` | yes | yes |  | yes | acted | yes |  |
| `ctrl+0` | yes | yes |  | yes | acted | yes |  |
| `ctrl+=` | yes | yes |  | yes |  | yes |  |
| `ctrl+-` | yes | yes |  | yes |  | yes |  |
| `ctrl+shift+=` | yes | yes |  | yes |  | yes |  |
| `ctrl+numpad_add` | yes | yes |  | yes |  | yes |  |
| `ctrl+numpad_subtract` | yes | yes |  | yes |  | yes |  |
| `ctrl+`` | yes | yes |  | yes | acted | yes |  |
| `ctrl+,` | yes | yes |  | yes | acted | yes |  |
| `ctrl+[` | yes | yes |  | yes |  | yes |  |
| `ctrl+]` | yes | yes |  | yes |  | yes |  |
| `ctrl+\` | yes | yes |  | yes | acted | yes |  |
| `ctrl+space` | yes | yes |  | yes |  | yes |  |
| `ctrl+enter` | yes |  |  | yes |  | yes |  |
| `ctrl+delete` | yes |  |  | yes |  | yes |  |
| `ctrl+backspace` | yes |  |  | yes |  | yes |  |
| `ctrl+tab` | yes | yes |  | yes | acted | yes |  |
| `ctrl+shift+tab` | yes | yes |  | yes | acted | yes |  |
| `ctrl+pageup` | yes | yes |  | yes |  | yes |  |
| `ctrl+pagedown` | yes | yes |  | yes |  | yes |  |
| `ctrl+p` | yes [dp] | yes |  | yes [dp] |  | yes |  |
| `ctrl+shift+p` | yes [dp] | yes |  | yes [dp] | acted | yes |  |
| `ctrl+q` | yes | yes |  | yes | acted | yes |  |
| `ctrl+o` | yes | yes |  | yes | acted | yes |  |
| `ctrl+n` | yes | yes |  | yes | acted | yes |  |
| `ctrl+w` | yes | yes |  | yes |  CLOSED | yes |  |
| `ctrl+f4` | yes | yes |  | yes |  CLOSED | yes |  |
| `ctrl+k ctrl+s` | yes [dp] | yes |  | yes | acted | yes |  |
| `ctrl+k ctrl+o` | yes | yes |  | yes |  | yes |  |
| `ctrl+k z` | yes | yes |  | yes | acted | yes |  |
| `ctrl+k m` | yes | yes |  | yes | acted | yes |  |
| `f1` | yes | yes |  | yes | acted | yes |  |
| `f2` | yes | yes |  | yes |  | yes |  |
| `f3` | yes | yes |  | yes |  | yes |  |
| `f4` | yes | yes |  | yes |  | yes |  |
| `f5` | yes | yes |  | yes | acted | yes |  |
| `f6` | yes | yes |  | yes | acted | yes |  |
| `f7` | yes | yes |  | yes |  | yes |  |
| `f8` | yes | yes |  | yes |  | yes |  |
| `f9` | yes | yes |  | yes |  | yes |  |
| `f10` | yes | yes |  | yes |  | yes |  |
| `f11` | yes | yes |  | yes |  | yes |  |
| `f12` | yes | yes |  | yes |  | yes |  |
| `shift+f5` | yes | yes |  | yes |  | yes |  |
| `ctrl+f5` | yes | yes |  | yes | acted | yes |  |
| `shift+f10` | yes | yes |  | yes |  | yes |  |
| `shift+f11` | yes | yes |  | yes |  | yes |  |
| `ctrl+f2` | yes | yes |  | yes |  | yes |  |
| `ctrl+f11` | yes | yes |  | yes |  | yes |  |
| `ctrl+f12` | yes | yes |  | yes |  | yes |  |
| `alt+f` | yes |  | acted | yes | acted | yes |  |
| `alt+e` | yes |  | acted | yes | acted | yes |  |
| `alt+v` | yes |  | acted | yes | acted | yes |  |
| `alt+p` | yes |  |  | yes |  | yes |  |
| `alt+i` | yes |  |  | yes |  | yes |  |
| `alt+t` | yes |  | acted | yes | acted | yes |  |
| `alt+r` | yes |  | acted | yes | acted | yes |  |
| `alt+s` | yes |  | acted | yes | acted | yes |  |
| `alt+h` | yes |  | acted | yes | acted | yes |  |
| `alt+3` | yes |  |  | yes | acted | yes |  |
| `alt+1` | yes |  | acted | yes | acted | yes |  |
| `alt+2` | yes |  |  | yes | acted | yes |  |
| `alt+a` | yes |  |  | yes |  | yes |  |
| `alt+z` | yes |  |  | yes |  | yes |  |
| `alt+x` | yes |  |  | yes |  | yes |  |
| `alt+d` | yes |  |  | yes |  | yes |  |
| `alt+left` | yes |  |  | yes | acted | yes |  |
| `alt+right` | yes |  |  | yes |  | yes |  |
| `alt+up` | yes |  |  | yes |  | yes |  |
| `alt+down` | yes |  |  | yes |  | yes |  |
| `alt+space` | yes |  | acted | yes | acted | yes |  |
| `alt+enter` | yes |  |  | yes |  | yes |  |
| `alt+shift+f` | yes |  | acted | yes | acted | yes |  |
| `ctrl+alt+f` | yes [dp] |  |  | yes [dp] |  | yes |  |
| `ctrl+alt+r` | yes |  |  | yes |  | yes |  |
| `ctrl+alt+i` | yes | yes |  | yes | acted | yes |  |
| `ctrl+alt+b` | yes | yes |  | yes |  | yes |  |
| `ctrl+alt+p` | yes [dp] | yes |  | yes [dp] |  | yes |  |
| `alt` | yes |  | acted | yes | acted | yes |  |
| `shift` | yes |  |  | yes |  | yes |  |
| `ctrl` | yes |  |  | yes |  | yes |  |
| `capslock` | yes |  |  | yes |  | yes |  |
| `numlock` | yes |  |  | yes |  | yes |  |
| `scrolllock` | yes |  |  | yes |  | yes |  |
| `apps` | yes |  | acted | yes | acted | yes |  |
| `pause` | yes |  |  | yes |  | yes |  |
| `win+shift+s` | no |  |  | no | acted | no |  |
| `pointerlock` | yes |  |  | no |  | yes |  |
| `fullscreen-element` | yes |  |  | yes |  | yes |  |
| `ctrl+shift+n` | yes |  | acted | yes | acted | yes | acted |
| `ctrl+shift+w` | yes |  |  | yes |  | yes |  |

Totals over 215 chords: A saw 214, rescued 85, VS Code acted 14; B saw 213, VS Code acted 54, closed 2; C saw 214, VS Code acted 1.

`win+shift+s` was deliberately not sent (Snipping Tool). `ctrl+shift+n` / `ctrl+shift+w` were sent last, after the custom-editor tests, because they create / close windows; in B `ctrl+shift+n` opened a second window which the driver closed with Alt+F4, in A and C (iframe / rescued) no new window appeared. Rows flagged `acted` in A for `alt+<menu letter>`, `alt`, `alt+space`, `apps` are the custom title bar menu mnemonics, the Alt menu-focus toggle, the system menu and the context-menu key: they are not keybindings and cannot be rescued with `contributes.keybindings` (see the findings file for the settings that disable them).
