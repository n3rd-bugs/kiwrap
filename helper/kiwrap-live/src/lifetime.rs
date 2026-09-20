//! Self-termination when the launcher (the VS Code extension host or `kw_live`) dies.
//!
//! Two mechanisms, belt and braces:
//! 1. a kill-on-close Job Object that this process assigns ITSELF to, whose only remaining
//!    handle is duplicated INTO the parent process: when the parent exits (orderly or not) the
//!    OS closes that handle, the job loses its last handle and
//!    `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` terminates the helper even if it is wedged;
//! 2. a thread waiting on the parent's process handle that exits cleanly (stops captures) first.
//! The helper never owns the KiCad process (the launcher does, docs/DISPLAY.md 7.1).

use windows::Win32::Foundation::{CloseHandle, DUPLICATE_SAME_ACCESS, HANDLE};
use windows::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W, TH32CS_SNAPPROCESS,
};
use windows::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation, SetInformationJobObject,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
use windows::Win32::System::Threading::{
    GetCurrentProcess, GetCurrentProcessId, OpenProcess, WaitForSingleObject, INFINITE, PROCESS_DUP_HANDLE,
    PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_SYNCHRONIZE,
};
use windows::Win32::Foundation::DuplicateHandle;

/// Parent PID of the current process (Toolhelp snapshot).
pub fn parent_pid() -> Option<u32> {
    unsafe {
        let me = GetCurrentProcessId();
        let snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0).ok()?;
        let mut e = PROCESSENTRY32W { dwSize: std::mem::size_of::<PROCESSENTRY32W>() as u32, ..Default::default() };
        let mut found = None;
        if Process32FirstW(snap, &mut e).is_ok() {
            loop {
                if e.th32ProcessID == me {
                    found = Some(e.th32ParentProcessID);
                    break;
                }
                if Process32NextW(snap, &mut e).is_err() {
                    break;
                }
            }
        }
        let _ = CloseHandle(snap);
        found
    }
}

/// Install both guards for `parent`. Returns a description of what was set up.
pub fn guard(parent: u32, on_exit: impl Fn() + Send + 'static) -> String {
    unsafe {
        let ph = match OpenProcess(PROCESS_SYNCHRONIZE | PROCESS_DUP_HANDLE | PROCESS_QUERY_LIMITED_INFORMATION, false, parent) {
            Ok(h) => h,
            Err(e) => return format!("parent {parent}: OpenProcess failed ({e}); no lifetime guard"),
        };
        let mut what = String::new();
        // 1. job object, handle moved into the parent
        match CreateJobObjectW(None, None) {
            Ok(job) => {
                let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                let ok = SetInformationJobObject(
                    job,
                    JobObjectExtendedLimitInformation,
                    &info as *const _ as *const _,
                    std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                )
                .is_ok()
                    && AssignProcessToJobObject(job, GetCurrentProcess()).is_ok();
                if ok {
                    let mut dup = HANDLE::default();
                    if DuplicateHandle(GetCurrentProcess(), job, ph, &mut dup, 0, false, DUPLICATE_SAME_ACCESS).is_ok() {
                        let _ = CloseHandle(job); // the parent now holds the only handle
                        what.push_str("job(kill-on-close, handle in parent) ");
                    } else {
                        what.push_str("job(assigned, duplicate failed) ");
                    }
                } else {
                    let _ = CloseHandle(job);
                    what.push_str("job(unavailable) ");
                }
            }
            Err(_) => what.push_str("job(create failed) "),
        }
        // 2. waiter thread
        let raw = ph.0 as usize;
        std::thread::Builder::new()
            .name("parent-watch".into())
            .spawn(move || {
                let h = HANDLE(raw as *mut _);
                let _ = WaitForSingleObject(h, INFINITE);
                log::info!("parent process {parent} exited; stopping");
                on_exit();
                std::thread::sleep(std::time::Duration::from_millis(400));
                std::process::exit(0);
            })
            .ok();
        what.push_str("waiter");
        what
    }
}
