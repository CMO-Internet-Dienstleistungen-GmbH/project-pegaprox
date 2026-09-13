# Moving a Hyper-V disk: what it costs and what it does when it fails

Scope: what one import asks of the two hosts involved, measured rather than estimated, and
what happens when the target runs out of room or the conversion is interrupted. Fork issue
#36 is the requirement; `pegaprox/core/hyperv_transfer.py` explains why the transfer has
the shape it has.

Nothing here names a real host, account, network or customer. Every number describes the
product or the load it is under.

## Status

| Item | State |
|---|---|
| Virtual size and physical size told apart, both ends | **Measured** — a 64 GiB disk holding 256 MiB, and the same disk holding 4 GiB |
| Memory of the converting process against disk size | **Measured** — ~24 MiB either way |
| Memory and output kept by PegaProx while it watches | **Measured and locked** — the peak does not grow between 2 000 progress reads and 200 000 (`test_a_hundred_times_the_progress_costs_no_more_memory`) |
| Remote calls against disk size | **Measured and locked** — 7 per run at 40 GiB and at 4 TiB (`test_one_disk_costs_these_seven_calls_and_no_others`) |
| A target that runs out of room | **Measured** — non-zero exit, the byte it stopped at, partial volume freed |
| An interrupted conversion and the retry after it | **Measured** — `tests/test_hyperv_xhm.py`, fresh volume per attempt |
| Throughput against a real Hyper-V host over SMB | **Not measured** — needs a Hyper-V host; see *Limits* |
| Several TB in one disk | **Not measured** — the largest disk actually converted here is 64 GiB virtual |

## What was measured, and on what

A sparse VHDX of a deliberately awkward shape: 64 GiB virtual, with data at both ends and
holes between them, so a copy that stops early is visibly short rather than merely smaller.
It is read over a read-only SMB share with the product's own mount options and converted
with the product's own `qemu-img` command — both taken from `hyperv_transfer.py` at run
time rather than retyped.

Run it with:

```bash
PYTHON=/path/to/python tests/hyperv_testbed/measure_large_import.sh
HYPERV_LARGE_CONTENT_MB=4096 HYPERV_FULL_TARGET_MB=512 \
  PYTHON=/path/to/python tests/hyperv_testbed/measure_large_import.sh
```

Measured with **qemu-img 9.0.2** on Alpine 3.20, source and target both local to the
converting container. The durations below therefore say nothing about a network; they are
in the table because leaving them out would be worse, not because they predict anything.

### A disk with room to land in

| | 64 GiB virtual, 256 MiB content | 64 GiB virtual, 4 GiB content |
|---|---|---|
| Source virtual size | 68 719 476 736 bytes | 68 719 476 736 bytes |
| Source size on the share | 276 824 064 bytes | 4 303 355 904 bytes |
| `qemu-img` exit code | 0 | 0 |
| Peak resident memory of `qemu-img` | 23 936 KiB | 24 092 KiB |
| Duration | 1 s | 5 s |
| Target apparent size | 68 719 476 736 bytes | 68 719 476 736 bytes |
| Target allocated | 262 148 KiB | 4 194 308 KiB |
| Content | matches the fixture checksum | matches the fixture checksum |

One run, reported as it came back. Repeating it moves the peak by tens of kilobytes and the
duration by a second, so read the columns against each other rather than as constants.

Two things are worth reading off that table.

**Sixteen times the content costs sixteen times the allocation and nothing else.** The
target is written sparse, so it allocates what the source actually held, not what the
source claimed. A transfer sized by the virtual figure would have moved 64 GiB in both
columns.

**The converting process does not grow with the disk.** It peaked at roughly 24 MiB for
both, which is what makes the claim in `hyperv_transfer.py` — one pass, no buffer
proportional to the disk — something other than an assertion.

### A target that runs out

The same disk, onto a target too small to hold it:

| | 64 MiB target | 512 MiB target |
|---|---|---|
| `qemu-img` exit code | 1 | 1 |
| Peak resident memory of `qemu-img` | below the sampling interval | 24 032 KiB |
| Target allocated when it stopped | 65 536 KiB | 524 288 KiB |
| What it said | `error while writing at byte 67108864: No space left on device` | `error while writing at byte 536870912: No space left on device` |

It stops at the byte it could not write, says so, and exits non-zero. It does not truncate
quietly and it does not leave a target that would mount. What PegaProx does with that
failure is covered by tests rather than by this script: the partial volume is freed
(`test_a_failed_conversion_frees_its_partial_volume`), the share is unmounted even though
the conversion failed (`test_the_mount_is_undone_even_when_the_conversion_fails`), and a
retry allocates a fresh volume rather than writing into the half-written one
(`test_a_conversion_is_retried_on_a_fresh_volume`).

**The source is never part of any of it.** A failed import leaves the Hyper-V VM exactly as
it found it, which is the one property this direction may not trade away.

## What PegaProx itself holds while a transfer runs

The data does not pass through PegaProx. What does pass through is a progress percentage,
and a reader that appended it would grow with the length of the conversion rather than with
its size — a multi-hour import would leave a management server holding megabytes of
carriage returns.

Measured against the real reader (`_Node.run_with_progress`), with `tracemalloc` around
the call:

| Progress reads | Characters the conversion produced | Characters kept | Peak allocation of the reader |
|---|---|---|---|
| 2 000 | 34 011 | 18 | 4 656 bytes |
| 200 000 | 3 380 021 | 18 | 4 656 bytes |

Every one of the 200 000 updates was reported to the caller; none was dropped to achieve
that. The identical peak at a hundred times the volume is the point: what is kept is the
last chunk, not the history.

**What a test holds, and what it does not.** The byte figures above are one run on one
interpreter and are not asserted as such — a peak allocation is not a stable number across
Python builds. What `test_a_hundred_times_the_progress_costs_no_more_memory` does assert is
the invariant they illustrate: the peak of the large run is not larger than the small one's,
so a reader that started accumulating would fail rather than quietly outgrow this table.
The character counts are held separately by `test_two_hundred_thousand_progress_reads_keep_one_chunk`.

A cancellation closes the channel rather than reading to the end of a conversion nobody
wants any more (`test_a_cancelled_conversion_stops_reading_and_closes_the_channel`).

## What one import asks of the target node

Counted from the commands the runner actually issued:

| Run | Commands over SSH | Mounts | Conversions |
|---|---|---|---|
| One disk, 40 GiB | 7 | 1 | 1 |
| One disk, 4 TiB | 7 | 1 | 1 |
| Two disks on one drive, 40 GiB each | 11 | 1 | 2 |

Every figure in that table is asserted, not observed once:
`test_one_disk_costs_these_seven_calls_and_no_others` names the seven in order,
`test_two_disks_on_one_drive_cost_eleven_calls_and_one_mount` holds the eleven and the
single mount, and `test_the_number_of_remote_calls_does_not_depend_on_the_disk` holds the
part that matters most — that a disk a hundred times the size costs the same seven.

The seven are: mount the share, check the file is readable and read its size, allocate the
volume, resolve the volume's device path, convert, unmount, remove the credentials file.
Writing that credentials file is one further command, issued once when the connection to
the node is opened and counted separately here because the measurement replaces that step.
A disk a hundred times the size costs the same seven, because the node is told once what to
convert and then only watched. A second disk on the same drive costs four more and **no
second mount** — the share is mounted per share, not per disk.

The credentials never appear in any of those commands. The file is created with
`umask 077 && cat > <path>` and its content arrives on standard input, so the password is
never in a command line where every user on the node could read it out of the process list
(`test_the_share_credentials_never_appear_in_a_command`).

## While it runs

The start request returns `202 Accepted` as soon as the migration is recorded, and the
transfer runs in a daemon thread — the same shape upstream already uses for every other
cross-hypervisor direction, reused rather than reinvented. No request waits on a
conversion, so the length of an import is not the length of an HTTP call.

Progress reaches the browser the way the other directions' does: the runner sets a
percentage per disk, the panel polls for it. What is **not** measured is a browser watching
a multi-hour import from beginning to end; what has been measured in a browser is the
wizard and the migration panel against a running instance, which is what #34 and #35
required.

## Two sources at once

Each Hyper-V host numbers its own target VMs and holds its own claim, so an import running
on one host neither blocks nor renumbers an import on another
(`TestTwoSourcesSideBySide`). Within one host, a second import of the same VM is refused
rather than queued — see `docs/hyperv-migration.md`.

## Limits

Said plainly, because the alternative is a number somebody plans a maintenance window
around:

- **There is no throughput figure here, and no estimate in the UI.** A duration measured
  between two local files on one workstation predicts nothing about a VHDX read over SMB
  from a production hypervisor. The product shows a percentage and an elapsed time; it does
  not show a projected finish.
- **The largest disk actually converted is 64 GiB virtual.** The 4 TiB row above counts
  commands, not bytes — it proves the call count is independent of the size, not that a
  4 TiB conversion has been run.
- **A broken transfer restarts that disk.** There is no resume, on purpose: the converter
  writes in the order the source's block table dictates, so a byte count says nothing about
  which parts of the target are valid.
- **Network interruption has been reproduced as a failed conversion, not as a severed
  SMB mount.** What is proven is that a non-zero exit frees the volume, unmounts the share
  and allows a clean retry. A mount that hangs rather than failing is a host-side
  behaviour and is owed a measurement on a real Hyper-V host.
- **Nothing here ran on Windows.** Every figure comes from the Docker testbed. The
  measurements this epic still owes a real Hyper-V system are listed in
  `docs/hyperv-console.md` and `docs/hyperv-verification.md`.
