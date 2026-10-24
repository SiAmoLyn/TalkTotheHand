# Nebula transport pilot — status: blocked in the agent's VM environment

An experiment in giving Talk to the Hand a self-hosted machine-to-machine
transport: a [Nebula](https://github.com/slackhq/nebula) overlay
(192.168.100.0/24) between the three machines the agent works across —
the cloud VM it lives in, and the operator's two Macs — so nodes could
later be reached without third-party relays.

**What was built and verified (2026-10-24/25):**

- Nebula v1.11.2 built from pinned upstream source for darwin/arm64 and
  linux/amd64; binaries hash-verified end to end.
- A private CA with one certificate per node (CA key kept offline on the
  build machine only; never committed anywhere).
- Configs in `configs/`: the home Mac mini as lighthouse, the VM and the
  company Mac as nodes. All configs pass `nebula -test`; every node
  starts and brings its overlay interface up.

**The pilot is nevertheless blocked. The problems below occur when the
agent (Sisi) uses this from its Muse VM — they are properties of that
hosting environment, not of Nebula.**

## Known issues

### 1. BLOCKING — the VM has no direct data path to the other machines

The VM's processes cannot exchange direct traffic with the operator's
Macs over the existing tailnet, in either direction:

- Mac node → VM lighthouse: handshake messages sent, **all timed out**.
- Topology flipped (Mac as lighthouse, VM as initiating node): the VM's
  handshakes **also all timed out**.
- Raw probes agree: UDP and TCP sent from a Mac to the VM's tailnet
  address never arrive at a listening socket in the VM, and the VM's
  agent shell cannot even send toward tailnet addresses
  (`sendto`/`ping`: *operation not permitted*).

The only working paths between the VM and the Macs are intermediary
ones: a platform proxy (used for SSH) and the vendor's cloud gateway.
**Consequence:** any direct-connection overlay (Nebula, or any UDP
mesh) with this VM as an endpoint cannot work in this environment.
Reviving the pilot requires a publicly reachable lighthouse/relay
(a small VPS), which is a deliberate operator decision — not deployed.

### 2. Deployment bug (fixed) — certificate paths are bare filenames

The generated configs name `ca` / `cert` / `key` as bare filenames,
resolved against the process working directory. Laying certificates
out in a `pki/` subdirectory — the layout the build produced — fails
at startup with `unable to read pki.key file`. Certificates must sit
**flat beside the config** in the working directory (see `configs/`
header comments).

### 3. Operations note — background processes on the VM are fragile

Long-running processes started from an agent session on the VM can be
killed by runtime restarts without notice. Daemons here need
`setsid`/service supervision **and** liveness verification; "started"
is not "still running".

### 4. Untested leg

Mac ↔ Mac overlay traffic was not exercised (the two machines were
never online with the overlay at the same time). Nothing about the
blocking issue above applies to that leg — both are ordinary devices
with a working direct tailnet path.

## Current state

- VM node: stopped (it provably cannot connect).
- Mac mini lighthouse: left running, inert without reachable nodes.
- The hand's production traffic continues over the pre-existing SSH
  channel; this experiment changes nothing about it.
