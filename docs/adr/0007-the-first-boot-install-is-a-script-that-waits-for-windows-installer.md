# ADR 0007 — The first-boot install is a script that waits for Windows Installer

**Status:** accepted, verified on a Windows Server 2022 guest
**Date:** 2026-09-19
**Scope:** fork issue #15 (Hyper-V migration source); shared with the VMware direction,
because the injection in `pegaprox/core/v2p.py` is shared

## Context

The offline VirtIO injection stages two installers in `C:\qemu`: `virtio-win-gt-x64.msi`
(the drivers) and `qemu-ga-x86_64.msi` (the guest agent). It also registers a one-shot
service, `PegaProxFirstBoot`, to install them at first boot. Until now the service's
`ImagePath` was a single `cmd.exe /c` chain that ran both installers, armed the boot
drivers and deleted the service.

Observed on migrated guests:

- The two installer logs start two to three seconds apart and overlap. Which of the two
  fails varies, and some runs succeed.
- One failed agent install ended with `RegisterCom failed Error code: -2147023841`
  (0x8007041F, `ERROR_SERVICE_DATABASE_LOCKED`) and 1708.
- Putting `start "" /wait` in front of each `msiexec` did not change any of this.

Two facts from Microsoft's documentation apply:

- `StartService`: *"A service cannot call StartService during initialization. The reason
  is that the SCM locks the service control database during initialization."*
  `cmd.exe` never calls `StartServiceCtrlDispatcher`, so the service stays in
  initialization until the SCM times out on it. Everything in the chain ran during that
  time.
- `_MSIExecute` mutex: Windows Installer holds `Global\_MSIExecute` while it processes an
  execute sequence. A second installation that starts while the mutex is held fails with
  1618.

Why `start /wait` did not keep the two installers apart is **not established**. No log of
a run with it has been analysed here.

## Options

**A — keep the chain, add delays.** Rejected: a delay long enough for one guest is too
short for a slower one, and it waits for time rather than for a condition.

**B — run the installs from a scheduled task instead of a service.** Task Scheduler
provides a proper boot trigger. Rejected: creating a task offline means writing the
TaskCache registry structures and the task XML by hand, which is much more to get wrong
than the service already in place.

**C — the service only launches a script, and the script waits on observable state.**
The service runs `cmd.exe /c start "" powershell.exe … -File C:\qemu\firstboot.ps1`
and returns within a second, so no service is initializing while the installers run.
The script:

- waits until `PegaProxFirstBoot` is no longer start-pending;
- waits until `Global\_MSIExecute` is free, both before each installer and after it,
  so a `msiexec` process that exits does not count as a finished installation;
- retries a failed installer up to three times;
- writes a timestamped log to `C:\Windows\Temp\pegaprox-firstboot.log`, which stays
  after a successful run so the order of events can be checked on any guest.

## Decision

Option C. It does not depend on the unexplained `/wait` behaviour, and it removes the one
documented cause of `ERROR_SERVICE_DATABASE_LOCKED` that applied to the old chain.

The script lives in `pegaprox/core/virtio_firstboot.py` and reaches the node base64
encoded, so it does not have to survive the heredocs it is nested in. `v2p.py` carries
only the hooks. The script is written in PowerShell 2.0 syntax, the version Server
2008 R2 ships with.

## Consequences

- The guest now needs `powershell.exe`. Every Windows release this import supports ships
  with it.
- The SCM logs the service as having stopped without reporting. It has `ErrorControl 0`,
  so nothing else follows from that.
- A guest keeps `C:\Windows\Temp\pegaprox-firstboot.log`. The staging folder `C:\qemu` is
  still removed after a clean run and kept, with its MSI logs, after a failed one.
- `tests/test_virtio_firstboot.py` runs the script under `pwsh` against a fake `msiexec`
  that returns while its installation still holds the mutex. With the wait removed, the
  test shows the overlap.

## Verification

A Windows Server 2022 guest, imported from Hyper-V onto RBD storage with virtio-win
0.1.302 (guest agent 110.2.3), was injected once and booted from the same snapshot each
time.

- **This design, three boots:** all three succeeded. In every run the MsiInstaller event
  1042 (transaction ended) for the driver MSI came before event 1040 (transaction started)
  for the agent MSI. Both installs returned 0 on the first attempt, `QEMU-GA` and
  `BalloonService` were running, and `C:\qemu` was removed. The agent answered 77 to 96 s
  after the VM started.
- **The previous `cmd /c` chain, one boot as a control:** the agent did not answer within
  ten minutes. The driver MSI succeeded. The agent MSI ended with Error 1722 in
  `RegisterCom`, actual error code -2147023841 (0x8007041F,
  `ERROR_SERVICE_DATABASE_LOCKED`), and the chain left `install-failed` behind.

In that control run the two installs did **not** overlap: the agent's log starts in the
same second the driver's log stops. So the chain fails even when `/wait` holds, and it
fails with the error code the SCM lock explains. The overlap seen on other guests was not
reproduced here, and why `/wait` did not prevent it there is still not established. The
mutex wait covers that case in the unit tests; the launcher removes the lock.
