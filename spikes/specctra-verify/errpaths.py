import sys, os, pcbnew
print("version", pcbnew.GetBuildVersion())
def t(label, fn):
    try:
        r = fn(); print(label, "-> returned", r)
    except Exception as ex:
        print(label, "-> EXCEPTION", type(ex).__name__, str(ex)[:200])
b = pcbnew.LoadBoard("test_locked.kicad_pcb")
t("SES missing file", lambda: pcbnew.ImportSpecctraSES(b, "nonexistent.ses"))
t("SES malformed", lambda: pcbnew.ImportSpecctraSES(b, "bad.ses"))
t("SES unknown ref R99", lambda: pcbnew.ImportSpecctraSES(b, "badref.ses"))
print("tracks after failed imports:", len(b.GetTracks()))
b2 = pcbnew.LoadBoard("test_nooutline.kicad_pcb")
t("DSN no outline", lambda: pcbnew.ExportSpecctraDSN(b2, "nooutline.dsn"))
print("nooutline.dsn exists:", os.path.exists("nooutline.dsn"))
t("DSN to unwritable dir", lambda: pcbnew.ExportSpecctraDSN(b, "Z:/nope/x.dsn"))
