# 3. A driver the loader refuses is not made boot-critical

Date: 2026-09-15

## Status

Accepted.

## Context

Windows Server 2012 R2 stops at the boot manager with:

```
File:   \Windows\system32\drivers\viostor.sys
Status: 0xc0000428
Info:   The operating system couldn't be loaded because the digital signature
        of a file couldn't be verified.
```

The reason is not in this product. Red Hat stopped having the virtio-win drivers for
out-of-support Windows versions signed through Microsoft. Read off the files with
`osslsigncode`:

| virtio-win release | signer of `viostor/2k12R2` |
|---|---|
| 0.1.190 | `Symantec Class 3 SHA256 Code Signing CA - G2` |
| 0.1.208 | `Symantec Class 3 SHA256 Code Signing CA - G2` |
| 0.1.221 | `virtio-win / Red Hat Inc.` (self-signed) |
| 0.1.240 and newer | `virtio-win / Red Hat Inc.` (self-signed) |

`0.1.208` is the last release whose 2012 R2 drivers carry a certificate chaining to
`Microsoft Code Verification Root`. The variants for Server 2016 and newer are unaffected;
they are signed by `Microsoft Windows Third Party Component CA 2014`.

A self-signed driver cannot be a boot driver on 64-bit Windows. Registering it as one
produces a machine that stops before the kernel starts — strictly worse than leaving the
guest on the controller it arrived on, which boots.

## Decision

Before a storage driver is registered as boot-start, the injection reads the PE
certificate table of the file it just copied and requires a signer the loader accepts:
`Microsoft Code Verification Root`, `Microsoft Windows Third Party Component CA` or
`Microsoft Windows Hardware Compatibility Publisher`.

The check reads the file rather than deciding from the Windows version, because an
operator who supplies an older driver ISO for an old guest has a driver that does load,
and a rule based on the build number would refuse it.

It is done with the standard library only — a PE header walk to data directory 4 — because
the node the injection runs on must not have packages installed on it for this.

When the check fails, the injection does not register the driver, reports why, and the
Hyper-V migration moves the VM back to its compatible controller so that it boots.

## Consequences

- No migration can produce a guest that stops at `0xc0000428` any more. The worst case is
  a guest on SATA with the drivers staged inside it, which is a machine that runs.
- Windows Server 2012 R2 reaches VirtIO SCSI when the operator points `virtio_iso_path` at
  virtio-win 0.1.208 or older. That is a supported configuration, not a workaround: the
  field exists for exactly this. Measured: with that ISO the guest is at its login screen
  a minute after starting, on `virtio-scsi-single`, with no manual step.
- The three accepted signer names are a list that could go stale if Microsoft introduces a
  new kernel-mode signing chain. It fails closed — an unrecognised chain means the guest
  is left on hardware that boots — and the log names the file, so the cause is visible.
