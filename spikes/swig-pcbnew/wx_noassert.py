"""Disable wxWidgets assert dialogs in a standalone KiCad python process (Windows).
wxDisableAsserts() is inline (wxTheAssertHandler = NULL); the global is exported from the wx base DLL,
so we null it via ctypes. Must be called AFTER `import pcbnew` (which loads the wx DLLs)."""
import ctypes, ctypes.wintypes as wt, os, glob, sys

def disable_wx_asserts(kicad_bin=None):
    kicad_bin = kicad_bin or os.path.dirname(sys.executable)
    dlls = glob.glob(os.path.join(kicad_bin, "wxbase*_vc*_custom.dll"))
    dlls = [d for d in dlls if "_net_" not in d and "_xml_" not in d]
    if not dlls:
        raise RuntimeError("wx base dll not found in " + kicad_bin)
    dll = dlls[0]
    k32 = ctypes.windll.kernel32
    k32.GetModuleHandleW.restype = wt.HMODULE
    k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    h = k32.GetModuleHandleW(os.path.basename(dll))      # already loaded by _pcbnew.pyd
    if not h:
        h = ctypes.WinDLL(dll)._handle
    k32.GetProcAddress.restype = ctypes.c_void_p
    k32.GetProcAddress.argtypes = [wt.HMODULE, ctypes.c_char_p]
    sym = b"?wxTheAssertHandler@@3P6AXAEBVwxString@@H000@ZEA"
    addr = k32.GetProcAddress(h, sym)
    if not addr:
        raise RuntimeError("wxTheAssertHandler export not found in " + dll)
    p = ctypes.cast(addr, ctypes.POINTER(ctypes.c_void_p))
    old = p.contents.value
    p.contents.value = None
    return dll, old

if __name__ == "__main__":
    import pcbnew
    print(disable_wx_asserts())
