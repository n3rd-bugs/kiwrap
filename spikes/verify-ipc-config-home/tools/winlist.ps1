# Usage: winlist.ps1 -Pid <pid>   -> lists visible top-level windows (title, class) owned by that PID
param([int]$ProcId)
Add-Type @"
using System; using System.Text; using System.Runtime.InteropServices; using System.Collections.Generic;
public class W {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr h, uint cmd);
  public static List<string> List(uint want) {
    var r = new List<string>();
    EnumWindows((h,l)=>{ uint pid; GetWindowThreadProcessId(h, out pid);
      if(pid==want && IsWindowVisible(h)){ var t=new StringBuilder(512); GetWindowText(h,t,512); var c=new StringBuilder(256); GetClassName(h,c,256);
        IntPtr owner = GetWindow(h, 4); r.Add(String.Format("hwnd={0} owner={1} class={2} title=[{3}]", h, owner, c, t)); }
      return true; }, IntPtr.Zero);
    return r; }
}
"@
[W]::List([uint32]$ProcId) | ForEach-Object { Write-Output $_ }
