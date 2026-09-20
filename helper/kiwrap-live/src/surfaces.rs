//! Surface model: the pure (Win32-free, unit-tested) grouping of a KiCad process's top-level
//! windows into *surfaces*, one per editor frame.
//!
//! Measured process model (docs/DISPLAY.md 12.2, win-multi.md 1): a project session is ONE
//! `kicad.exe` process and ONE GUI thread whose editor frames (manager, schematic editor, PCB
//! editor, 3D viewer, footprint/symbol editors) are *unowned* sibling top-level windows of class
//! `wxWindowNR`; dialogs and the footprint chooser are *owned* (`GW_OWNER`) by their editor frame;
//! popup menus (`#32768`), combo drop-downs (`ComboLBox`) and tooltips (`tooltips_class32`) are
//! unowned transient windows. PID cannot tell the editors apart: the unit is the root HWND plus
//! its owner chain.

use serde::Serialize;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize)]
pub struct Rect {
    pub l: i32,
    pub t: i32,
    pub r: i32,
    pub b: i32,
}

impl Rect {
    #[allow(dead_code)]
    pub fn new(l: i32, t: i32, r: i32, b: i32) -> Self {
        Rect { l, t, r, b }
    }
    pub fn w(&self) -> i32 {
        self.r - self.l
    }
    pub fn h(&self) -> i32 {
        self.b - self.t
    }
    pub fn is_empty(&self) -> bool {
        self.w() <= 0 || self.h() <= 0
    }
    pub fn contains(&self, x: i32, y: i32) -> bool {
        x >= self.l && x < self.r && y >= self.t && y < self.b
    }
    pub fn contains_rect(&self, o: &Rect) -> bool {
        o.l >= self.l && o.t >= self.t && o.r <= self.r && o.b <= self.b
    }
    pub fn intersects(&self, o: &Rect) -> bool {
        self.l < o.r && o.l < self.r && self.t < o.b && o.t < self.b
    }
    pub fn union(&self, o: &Rect) -> Rect {
        Rect { l: self.l.min(o.l), t: self.t.min(o.t), r: self.r.max(o.r), b: self.b.max(o.b) }
    }
    pub fn intersection(&self, o: &Rect) -> Rect {
        Rect { l: self.l.max(o.l), t: self.t.max(o.t), r: self.r.min(o.r), b: self.b.min(o.b) }
    }
    pub fn as_array(&self) -> [i32; 4] {
        [self.l, self.t, self.r, self.b]
    }
    pub fn as_xywh(&self) -> [i32; 4] {
        [self.l, self.t, self.w(), self.h()]
    }
}

/// One visible top-level window of the target process (see `win::windows_of_pid`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WinInfo {
    pub hwnd: usize,
    pub title: String,
    pub class: String,
    /// DWM extended frame bounds, physical px.
    pub rect: Rect,
    pub owner: usize,
    /// 0 = topmost in EnumWindows order.
    pub z: usize,
    pub enabled: bool,
    /// DWMWA_CLOAKED flags (0 = drawn on the desktop).
    pub cloaked: u32,
    pub thread_id: u32,
    pub style: u32,
    pub exstyle: u32,
}

impl WinInfo {
    /// Popup menus, combo drop-downs, tooltips, wx tip windows: transient popups that must never
    /// get a per-window WGC session (capturing a menu window dismisses it, winstream.md 2a) and
    /// never become a surface root.
    pub fn is_fragile(&self) -> bool {
        is_fragile_class(&self.class)
    }
    pub fn is_tooltip(&self) -> bool {
        self.class.starts_with("tooltips_class") || self.class == "wxTipWindow"
    }
    pub fn is_menu(&self) -> bool {
        self.class == "#32768"
    }
}

pub fn is_fragile_class(class: &str) -> bool {
    class == "#32768" || class.starts_with("tooltips_class") || class == "ComboLBox" || class == "wxTipWindow"
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Kind {
    Pcb,
    Sch,
    #[serde(rename = "3d")]
    ThreeD,
    Mgr,
    Fp,
    Sym,
    Other,
}

impl Kind {
    pub fn as_str(&self) -> &'static str {
        match self {
            Kind::Pcb => "pcb",
            Kind::Sch => "sch",
            Kind::ThreeD => "3d",
            Kind::Mgr => "mgr",
            Kind::Fp => "fp",
            Kind::Sym => "sym",
            Kind::Other => "other",
        }
    }
}

/// Title patterns of KiCad's editor frames (10.0.6 and 10.99 measured; the manager's title is
/// "<project> — KiCad <version>", hence the catch-all last). Order matters: an editor title
/// such as "… — PCB Editor" never contains "KiCad", but keep the specific patterns first anyway.
const KIND_PATTERNS: &[(&str, Kind)] = &[
    ("PCB Editor", Kind::Pcb),
    ("Schematic Editor", Kind::Sch),
    ("3D Viewer", Kind::ThreeD),
    ("Footprint Editor", Kind::Fp),
    ("Symbol Editor", Kind::Sym),
    ("Pcbnew", Kind::Pcb),
    ("Eeschema", Kind::Sch),
    ("KiCad", Kind::Mgr),
];

pub fn kind_for_title(title: &str) -> Kind {
    for (pat, kind) in KIND_PATTERNS {
        if title.contains(pat) {
            return *kind;
        }
    }
    Kind::Other
}

/// A dialog (owned top-level) or transient popup composited onto its owner's surface.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct DialogInfo {
    pub hwnd: usize,
    pub title: String,
    pub class: String,
    /// Screen rect, physical px.
    pub rect_px: [i32; 4],
    /// Offset of the dialog inside the surface image (surface px).
    pub offset: [i32; 2],
    pub enabled: bool,
    /// true for menus / combo drop-downs (captured through a monitor crop, not per window).
    pub popup: bool,
}

/// One editor frame plus everything that rides on it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Surface {
    pub root: usize,
    pub kind: Kind,
    pub title: String,
    /// The root frame's own rect.
    pub root_rect: Rect,
    /// The rect the streamed image covers: the root rect extended to owned windows that
    /// intersect it (a dialog sticking out of the frame is shown whole; a window elsewhere on the
    /// desktop is gathered or ignored).
    pub rect: Rect,
    pub cloaked: u32,
    pub enabled: bool,
    pub thread_id: u32,
    /// Every member window in z-order, top first (root included).
    pub members: Vec<WinInfo>,
    pub dialogs: Vec<DialogInfo>,
}

impl Surface {
    /// The topmost enabled member (a modal dialog before its disabled frame): where keys go.
    pub fn focus_target(&self) -> usize {
        self.members.iter().find(|w| w.enabled && !w.is_fragile()).map(|w| w.hwnd).unwrap_or(self.root)
    }
    pub fn member_hwnds(&self) -> Vec<usize> {
        self.members.iter().map(|w| w.hwnd).collect()
    }
}

/// Group the windows of one process into surfaces.
///
/// Rules (win-multi.md 1, winstream `group_surfaces`):
/// 1. every unowned, titled, non-fragile window is a root (editor frame);
/// 2. every other window walks its `GW_OWNER` chain (max 16 hops) to a root;
/// 3. an unowned popup (menu, combo list) goes to the root whose rect contains its centre;
/// 4. tooltips are ignored; a popup outside every root belongs to no surface (a stray tooltip
///    parked on another monitor once grew a stage to 3.5 kpx, DISPLAY.md 12.3);
/// 5. the surface rect is the root rect unioned with owned non-fragile windows that intersect it.
pub fn group(wins: &[WinInfo]) -> Vec<Surface> {
    let by_hwnd: std::collections::HashMap<usize, &WinInfo> = wins.iter().map(|w| (w.hwnd, w)).collect();
    let mut roots: Vec<usize> = Vec::new();
    for w in wins {
        if w.owner == 0 && !w.is_fragile() && !w.title.is_empty() {
            roots.push(w.hwnd);
        }
    }
    let mut members: std::collections::HashMap<usize, Vec<&WinInfo>> =
        roots.iter().map(|r| (*r, Vec::new())).collect();
    for w in wins {
        if w.is_tooltip() {
            continue;
        }
        let mut h = w.hwnd;
        let mut hops = 0;
        while h != 0 && !members.contains_key(&h) && hops < 16 {
            h = by_hwnd.get(&h).map(|x| x.owner).unwrap_or(0);
            hops += 1;
        }
        if h == 0 || !members.contains_key(&h) {
            // unowned popup: geometric fallback
            let (cx, cy) = ((w.rect.l + w.rect.r) / 2, (w.rect.t + w.rect.b) / 2);
            h = 0;
            for r in &roots {
                if by_hwnd[r].rect.contains(cx, cy) {
                    h = *r;
                    break;
                }
            }
        }
        if h != 0 {
            members.get_mut(&h).unwrap().push(w);
        }
    }
    let mut out = Vec::new();
    for r in roots {
        let root = by_hwnd[&r];
        let mut mem = members.remove(&r).unwrap_or_default();
        mem.sort_by_key(|w| w.z);
        let mut rect = root.rect;
        for w in &mem {
            if w.hwnd != r && !w.is_fragile() && w.rect.intersects(&root.rect) {
                rect = rect.union(&w.rect);
            }
        }
        let dialogs = mem
            .iter()
            .filter(|w| w.hwnd != r)
            .map(|w| DialogInfo {
                hwnd: w.hwnd,
                title: w.title.clone(),
                class: w.class.clone(),
                rect_px: w.rect.as_array(),
                offset: [w.rect.l - rect.l, w.rect.t - rect.t],
                enabled: w.enabled,
                popup: w.is_fragile(),
            })
            .collect();
        out.push(Surface {
            root: r,
            kind: kind_for_title(&root.title),
            title: root.title.clone(),
            root_rect: root.rect,
            rect,
            cloaked: root.cloaked,
            enabled: root.enabled,
            thread_id: root.thread_id,
            members: mem.into_iter().cloned().collect(),
            dialogs,
        });
    }
    out
}

/// Stable ids: a root keeps its id for its lifetime; a new root of a kind takes the lowest free
/// suffix ("pcb", then "pcb2", ...).
#[derive(Default)]
pub struct IdMap {
    ids: std::collections::HashMap<usize, String>,
}

impl IdMap {
    pub fn assign(&mut self, surfaces: &[Surface]) -> Vec<(String, usize)> {
        let live: std::collections::HashSet<usize> = surfaces.iter().map(|s| s.root).collect();
        self.ids.retain(|h, _| live.contains(h));
        let mut out = Vec::new();
        for s in surfaces {
            if let Some(id) = self.ids.get(&s.root) {
                out.push((id.clone(), s.root));
                continue;
            }
            let base = s.kind.as_str();
            let mut n = 1;
            loop {
                let cand = if n == 1 { base.to_string() } else { format!("{base}{n}") };
                if !self.ids.values().any(|v| v == &cand) {
                    self.ids.insert(s.root, cand.clone());
                    out.push((cand, s.root));
                    break;
                }
                n += 1;
            }
        }
        out
    }
    pub fn id_of(&self, root: usize) -> Option<&String> {
        self.ids.get(&root)
    }
}

/// Owned dialogs that open outside their frame (KiCad remembers dialog positions: the footprint
/// chooser opened 1.6 kpx away over the manager, DISPLAY.md 12.2) are moved onto the frame once,
/// when first seen. Returns (hwnd, x, y) moves to perform.
pub fn gather_moves(s: &Surface, already: &std::collections::HashSet<usize>) -> Vec<(usize, i32, i32)> {
    let mut out = Vec::new();
    for w in &s.members {
        if w.hwnd == s.root || w.is_fragile() || w.owner == 0 || already.contains(&w.hwnd) {
            continue;
        }
        if s.root_rect.intersects(&w.rect) {
            continue;
        }
        let x = s.root_rect.l + ((s.root_rect.w() - w.rect.w()) / 2).max(0);
        let y = s.root_rect.t + ((s.root_rect.h() - w.rect.h()) / 2).max(0);
        out.push((w.hwnd, x, y));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn w(hwnd: usize, title: &str, class: &str, rect: Rect, owner: usize, z: usize) -> WinInfo {
        WinInfo {
            hwnd,
            title: title.into(),
            class: class.into(),
            rect,
            owner,
            z,
            enabled: true,
            cloaked: 0,
            thread_id: 7,
            style: 0,
            exstyle: 0,
        }
    }

    #[test]
    fn kinds_from_titles() {
        assert_eq!(kind_for_title("ecc83-pp.kicad_pcb [ecc83-pp/] — PCB Editor"), Kind::Pcb);
        assert_eq!(kind_for_title("ecc83.kicad_sch [ecc83/] — Schematic Editor"), Kind::Sch);
        assert_eq!(kind_for_title("ecc83-pp.kicad_pcb — 3D Viewer"), Kind::ThreeD);
        assert_eq!(kind_for_title("ecc83 — KiCad 10.0.6"), Kind::Mgr);
        assert_eq!(kind_for_title("Footprint Editor"), Kind::Fp);
        assert_eq!(kind_for_title("Symbol Editor"), Kind::Sym);
        assert_eq!(kind_for_title("Gerber Viewer"), Kind::Other);
    }

    #[test]
    fn project_session_groups_by_owner_chain() {
        // manager, sch, pcb, 3d as unowned siblings; a dialog owned by pcb; a chooser owned by pcb
        // far away; a menu (unowned) inside sch; a tooltip parked far away.
        let wins = vec![
            w(1, "Footprint Properties", "#32770", Rect::new(500, 500, 900, 900), 3, 0),
            w(2, "", "#32768", Rect::new(2100, 300, 2300, 500), 0, 1),
            w(3, "board — PCB Editor", "wxWindowNR", Rect::new(0, 0, 1920, 1080), 0, 2),
            w(4, "sch — Schematic Editor", "wxWindowNR", Rect::new(2000, 0, 3800, 1200), 0, 3),
            w(5, "Choose Footprint", "wxWindowNR", Rect::new(4000, 0, 4800, 800), 3, 4),
            w(6, "board — 3D Viewer", "wxWindowNR", Rect::new(100, 100, 1500, 1000), 0, 5),
            w(7, "proj — KiCad 10.0.6", "wxWindowNR", Rect::new(3000, 1300, 4000, 2000), 0, 6),
            w(8, "", "tooltips_class32", Rect::new(-5000, -5000, -4900, -4980), 0, 7),
        ];
        let s = group(&wins);
        assert_eq!(s.len(), 4);
        let pcb = s.iter().find(|x| x.root == 3).unwrap();
        assert_eq!(pcb.kind, Kind::Pcb);
        assert_eq!(pcb.member_hwnds(), vec![1, 3, 5]);
        assert_eq!(pcb.dialogs.len(), 2);
        assert_eq!(pcb.rect, Rect::new(0, 0, 1920, 1080)); // the far chooser does not grow it
        assert_eq!(pcb.dialogs[0].offset, [500, 500]);
        let sch = s.iter().find(|x| x.root == 4).unwrap();
        assert_eq!(sch.member_hwnds(), vec![2, 4]); // menu by geometry
        assert!(sch.dialogs[0].popup);
        assert!(s.iter().all(|x| !x.member_hwnds().contains(&8))); // tooltip dropped
        let mgr = s.iter().find(|x| x.root == 7).unwrap();
        assert_eq!(mgr.kind, Kind::Mgr);
        assert_eq!(s.iter().find(|x| x.root == 6).unwrap().kind, Kind::ThreeD);
    }

    #[test]
    fn dialog_sticking_out_grows_the_surface_rect() {
        let wins = vec![
            w(1, "Footprint Properties", "#32770", Rect::new(400, 900, 1400, 2100), 2, 0),
            w(2, "board — PCB Editor", "wxWindowNR", Rect::new(0, 1000, 1920, 2080), 0, 1),
        ];
        let s = group(&wins);
        assert_eq!(s[0].rect, Rect::new(0, 900, 1920, 2100));
        assert_eq!(s[0].dialogs[0].offset, [400, 0]);
    }

    #[test]
    fn modal_dialog_is_the_focus_target() {
        let mut frame = w(2, "board — PCB Editor", "wxWindowNR", Rect::new(0, 0, 1920, 1080), 0, 1);
        frame.enabled = false;
        let wins = vec![w(1, "Text Properties", "#32770", Rect::new(10, 10, 500, 500), 2, 0), frame];
        let s = group(&wins);
        assert_eq!(s[0].focus_target(), 1);
        assert!(!s[0].enabled);
    }

    #[test]
    fn ids_are_stable_and_suffixed() {
        let mut ids = IdMap::default();
        let a = vec![
            w(3, "a — PCB Editor", "wxWindowNR", Rect::new(0, 0, 10, 10), 0, 0),
            w(4, "b — PCB Editor", "wxWindowNR", Rect::new(0, 0, 10, 10), 0, 1),
        ];
        let s = group(&a);
        let first = ids.assign(&s);
        assert_eq!(first, vec![("pcb".to_string(), 3), ("pcb2".to_string(), 4)]);
        // root 3 goes away, root 4 keeps "pcb2", a new one gets "pcb"
        let b = vec![
            w(4, "b — PCB Editor", "wxWindowNR", Rect::new(0, 0, 10, 10), 0, 0),
            w(5, "c — PCB Editor", "wxWindowNR", Rect::new(0, 0, 10, 10), 0, 1),
        ];
        let s = group(&b);
        let second = ids.assign(&s);
        assert_eq!(second, vec![("pcb2".to_string(), 4), ("pcb".to_string(), 5)]);
    }

    #[test]
    fn gather_moves_only_far_owned_windows() {
        let wins = vec![
            w(1, "Choose Footprint", "wxWindowNR", Rect::new(4000, 0, 4800, 800), 3, 0),
            w(2, "Props", "#32770", Rect::new(100, 100, 500, 500), 3, 1),
            w(3, "board — PCB Editor", "wxWindowNR", Rect::new(0, 0, 1920, 1080), 0, 2),
        ];
        let s = group(&wins);
        let moves = gather_moves(&s[0], &Default::default());
        assert_eq!(moves, vec![(1, 560, 140)]);
        let mut seen = std::collections::HashSet::new();
        seen.insert(1);
        assert!(gather_moves(&s[0], &seen).is_empty());
    }

    #[test]
    fn cloaked_flag_is_reported() {
        let mut f = w(3, "a — PCB Editor", "wxWindowNR", Rect::new(0, 0, 10, 10), 0, 0);
        f.cloaked = 1;
        let s = group(&[f]);
        assert_eq!(s[0].cloaked, 1);
    }
}
