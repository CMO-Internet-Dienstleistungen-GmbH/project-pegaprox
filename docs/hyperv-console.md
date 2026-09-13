# The Hyper-V VM console

Scope: what the VMConnect console path is, how an operator provides what it needs, what
has been measured, and what is still owed a measurement. Fork issues #33, #34 and #35 are
the requirement.

Nothing here names a real host, account, network or customer.

## Status

| Item | State |
|---|---|
| Guacamole protocol client, against a real guacd | **Measured** — guacd 1.6.0, handshake completes and allocates a connection |
| Relay carrying that connection to a browser | **Measured** — `tests/hyperv_testbed/verify_console.sh` |
| Failure of an unreachable VMConnect endpoint | **Measured** — status 519, translated into a sentence an operator can act on |
| Authorization, per VM, with a single-use ticket | **Measured** — `tests/test_hyperv_console.py`, real access-control layer |
| VMConnect accepts `security=vmconnect` with a GUID blob | **Blocked** — needs a Hyper-V host |
| Generation 1 and Generation 2 firmware and boot visible | **Blocked** — needs a Hyper-V host |
| Keyboard input without a guest network | **Blocked** — needs a Hyper-V host |

The blocked rows are the acceptance criteria of #33. They need a released Hyper-V test
system and cannot be substituted: a successful connection to a stand-in proves the client
half, and says nothing about what Windows does with it.

**The named decision, should the blocked rows fail:** if a real host refuses
`security=vmconnect` from guacd, the console is not quietly dropped from the epic. Either
guacd gains what it needs (the RDP client is the part that would have to change), or the
console is replaced by a documented "open VMConnect on your own workstation" step, and
#34's scope changes in the open rather than by omission.

## What this console is

Hyper-V does not expose VNC or SPICE. A VM's console is served by the Virtual Machine
Connection endpoint on the host: RDP on port **2179**, with two differences from an
ordinary RDP session.

- The security mode is `vmconnect` rather than RDP's usual negotiation.
- The machine is named by its **GUID in the pre-connection blob**, not by an address.

That pair is what makes it a machine console rather than a remote desktop. It shows the
firmware, the boot loader, and an operating system that has no network — none of which an
RDP session into the guest could show, because all three happen before a guest could
answer one.

A guest's own RDP, the host's desktop, and the Hyper-V host shell are **not** substitutes
and are not offered as one.

## The path a console takes

```
browser ── websocket ──► PegaProx relay ── Guacamole protocol ──► guacd ── RDP/2179 ──► Hyper-V host
```

The browser is given a token and an address. Not the host, not the account, not the VM's
GUID: everything the connection actually needs is read on the server when the websocket
arrives. The relay makes three checks, in this order, because each makes the next
meaningful:

1. **The ticket is valid and single-use.** It expires in a minute and is spent by the first
   connection that presents it.
2. **The account may open this VM's console**, asked of the VM access-control layer, so a
   per-VM grant works and the neighbouring VM stays closed.
3. **The GUID is resolved from the identity map by VMID**, never read from the request. A
   caller cannot name a VM the access check was not asked about.

An open console is closed when the account behind it is disabled or deleted, on the next
30-second heartbeat, rather than living out the length of the browser tab.

## Providing guacd

The console needs guacd, the Guacamole proxy daemon, reachable from the PegaProx host. It
is the only component in this path that speaks RDP. **PegaProx runs without it**: every
other part of the product is unaffected, the console reports that it is missing, and
nothing else degrades.

Measured against **guacd 1.6.0**. Any 1.5-or-later build should do — the protocol version
guacd announces in its handshake (`VERSION_1_5_0` in 1.6.0) is answered with whatever it
names, so a newer daemon needs no change here.

The smallest reproducible deployment:

```yaml
# docker-compose.yml
services:
  guacd:
    image: guacamole/guacd:1.6.0
    restart: unless-stopped
    # Bound to loopback: nothing outside this host has any business reaching it.
    ports:
      - "127.0.0.1:4822:4822"
```

```bash
docker compose up -d
```

Then point PegaProx at it. Both values are environment variables, so they survive a
restart of either side and neither lives in a config file:

| Variable | Default | Meaning |
|---|---|---|
| `PEGAPROX_GUACD_HOST` | `127.0.0.1` | Where guacd listens |
| `PEGAPROX_GUACD_PORT` | `4822` | Its port |

**There are no secrets in any of this.** guacd is given the Hyper-V account for the
duration of one connection, from the encrypted cluster row, over a loopback socket. It
stores nothing, and neither does the console configuration.

What the deployment needs:

- The PegaProx host must reach **guacd**, and guacd must reach the **Hyper-V host on port
  2179**. In the compose file above guacd shares the PegaProx host's network, so the second
  rule is the PegaProx host's own firewall.
- The Hyper-V account configured for the source must be allowed to use Virtual Machine
  Connection. That is the same account the migration uses; no second credential exists.
- The browser must reach the relay, which listens on the PegaProx web port **+ 3**.

### Checking it

```bash
PYTHON=/path/to/python tests/hyperv_testbed/verify_console.sh
```

It starts guacd, completes the handshake with the product's own parameters, and asserts
that the failure of an unreachable VMConnect endpoint comes back as a sentence rather than
a number. With no Hyper-V host in reach, that failure **is** the expected result, and the
script says so.

## What a failure looks like

guacd reports a status code; PegaProx translates it. The codes it distinguishes are in
`pegaprox/core/hyperv_console.py`, and two are worth knowing:

- **519** covers three different things — nothing listening on 2179, a refused connection,
  and a failed TLS handshake — and only guacd's own text tells them apart. Measured against
  1.6.0. So the message says what is common to all three and carries guacd's text as the
  detail, rather than pretending to a precision it does not have.
- **A guacd that is not running at all** is reported before any of that, as itself, with
  the note that the rest of PegaProx is unaffected. It is the first failure an operator
  meets, because it is the state of a fresh installation.

## What is deliberately not here

- No host RDP desktop and no Hyper-V host shell. Those are ways into the host, not into
  the VM.
- No guest-network RDP. A console that needs the guest to be booted and reachable is not a
  boot console.
- No credentials in the browser, ever, and no reusable host credential anywhere near it.
