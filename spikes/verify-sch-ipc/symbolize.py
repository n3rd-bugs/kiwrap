"""Symbolize module-relative offsets in _eeschema.dll using dbghelp + the downloaded PDB."""
import ctypes, ctypes.wintypes as wt, os, sys

SP = os.path.dirname(os.path.abspath(__file__))
DLL = r"C:\Program Files\KiCad\10.0\bin\_eeschema.dll"
SYMDIR = os.path.join(SP, "sym")

dbghelp = ctypes.WinDLL("dbghelp")
SYMOPT_UNDNAME = 0x2
SYMOPT_LOAD_LINES = 0x10
SYMOPT_DEBUG = 0x80000000
dbghelp.SymSetOptions(SYMOPT_UNDNAME | SYMOPT_LOAD_LINES)
hproc = ctypes.c_void_p(0x1234)
dbghelp.SymInitializeW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, wt.BOOL]
if not dbghelp.SymInitializeW(hproc, SYMDIR, False):
    raise SystemExit("SymInitialize failed %d" % ctypes.get_last_error())
dbghelp.SymLoadModuleExW.restype = ctypes.c_ulonglong
dbghelp.SymLoadModuleExW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_ulonglong, wt.DWORD, ctypes.c_void_p, wt.DWORD]
BASE = 0x7FF700000000
base = dbghelp.SymLoadModuleExW(hproc, None, DLL, None, BASE, 0, None, 0)
if not base:
    raise SystemExit("SymLoadModuleEx failed %d" % ctypes.get_last_error())

class SYMBOL_INFOW(ctypes.Structure):
    _fields_ = [("SizeOfStruct", wt.ULONG), ("TypeIndex", wt.ULONG), ("Reserved", ctypes.c_ulonglong * 2),
                ("Index", wt.ULONG), ("Size", wt.ULONG), ("ModBase", ctypes.c_ulonglong), ("Flags", wt.ULONG),
                ("Value", ctypes.c_ulonglong), ("Address", ctypes.c_ulonglong), ("Register", wt.ULONG),
                ("Scope", wt.ULONG), ("Tag", wt.ULONG), ("NameLen", wt.ULONG), ("MaxNameLen", wt.ULONG),
                ("Name", ctypes.c_wchar * 2000)]

class IMAGEHLP_LINEW64(ctypes.Structure):
    _fields_ = [("SizeOfStruct", wt.DWORD), ("Key", ctypes.c_void_p), ("LineNumber", wt.DWORD),
                ("FileName", wt.LPWSTR), ("Address", ctypes.c_ulonglong)]

dbghelp.SymFromAddrW.argtypes = [ctypes.c_void_p, ctypes.c_ulonglong, ctypes.POINTER(ctypes.c_ulonglong), ctypes.POINTER(SYMBOL_INFOW)]
dbghelp.SymGetLineFromAddrW64.argtypes = [ctypes.c_void_p, ctypes.c_ulonglong, ctypes.POINTER(wt.DWORD), ctypes.POINTER(IMAGEHLP_LINEW64)]

def sym(off):
    si = SYMBOL_INFOW(); si.SizeOfStruct = 88; si.MaxNameLen = 2000
    disp = ctypes.c_ulonglong(0)
    name = "?"
    if dbghelp.SymFromAddrW(hproc, base + off, ctypes.byref(disp), ctypes.byref(si)):
        name = f"{si.Name}+0x{disp.value:x}"
    li = IMAGEHLP_LINEW64(); li.SizeOfStruct = ctypes.sizeof(IMAGEHLP_LINEW64)
    d2 = wt.DWORD(0)
    line = ""
    if dbghelp.SymGetLineFromAddrW64(hproc, base + off, ctypes.byref(d2), ctypes.byref(li)):
        line = f"  [{li.FileName}:{li.LineNumber}]"
    return f"_eeschema.dll+0x{off:x} -> {name}{line}"

for a in sys.argv[1:]:
    print(sym(int(a, 16)))
