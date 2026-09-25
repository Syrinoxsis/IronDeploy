# Driver Lifecycle Roadmap

This document describes the intended direction of IronDeploy driver handling.
It is a design roadmap, not a statement that the features below are already
implemented or a commitment to a particular release.

## Current implementation baseline

The first local-first slice is implemented:

- absent payloads declared in `SourceDisksFiles` are recorded as package
  warnings and make the bundle ineligible for native driver installation;
- `AUTO_LOCAL` and `AUTO_LOCAL_WSUS` run up to three authenticated local
  reconciliation passes in installed Windows before software installation;
- archives use the deployment SMB transport and are verified with SHA-256;
- Windows Update driver offers are held during the local passes and the prior
  policy value is restored afterwards;
- a final device/local-match report is stored in `driverResolution` and shown
  at the bottom of `/dashboard/{current_id}`;
- a requested reboot is deferred until all post-install work has finished.

Exact source provenance (distinguishing inbox, our copied package, and Windows
Update after Windows renames third-party INFs to `oemNN.inf`), an observed
Windows Update fallback window, and the complete INF install-section graph
remain roadmap work below.

The current `AUTO_LOCAL` flow resolves hardware visible in WinPE, builds a
deployment-specific archive from matching local bundles, and injects those
drivers into the offline Windows image. This keeps transfers small, but WinPE
cannot expose every device that will exist after the full operating system and
its parent drivers start. DCH software components, extension devices, audio
effects, and Bluetooth child devices can appear only during the first Windows
boot.

A production driver workflow therefore cannot rely on a single WinPE inventory
pass. The target design is local-first, multi-pass resolution with an explicit
and auditable Windows Update fallback.

## Goals

- Prefer administrator-provided drivers from IronAPI over Internet sources.
- Handle devices that materialize only after their parent driver is installed.
- Avoid transferring every enabled driver package to every deployment.
- Detect incomplete or inconsistent packages automatically during import.
- Make driver origin and unresolved hardware visible without manual log review.
- Preserve the existing `MANUAL_FOLDER` and `NO_DRIVERS` behavior unless a
  separately approved migration changes their contracts.

## Target flow

```text
Package upload
  -> automatic package validation and indexing
  -> publish an immutable, content-addressed snapshot

WinPE
  -> collect pre-install PnP inventory
  -> resolve local candidates
  -> download and inject the deployment snapshot

First Windows boot
  -> hold automatic Windows Update driver retrieval
  -> collect the complete PnP inventory
  -> resolve and install missing local candidates
  -> repeat inventory/resolution until stable or the pass limit is reached
  -> record unresolved devices
  -> optionally release Windows Update fallback

Completion
  -> report local-offline, local-firstboot, inbox, Windows Update, and
     unresolved outcomes per device
```

## Phase 1: accurate package validation

The INF indexer should eventually distinguish files required by a reachable
installation path from unused declarations. Current evidence from offline DISM
shows that even an apparently unreachable missing `SourceDisksFiles` payload
can reject the package, so the safe current policy is to withhold such bundles.

- Build a reachability graph from applicable model install sections through
  `CopyFiles`, `AddService`, service binaries, co-installers, catalogs, and
  supported component directives.
- Treat a missing reachable payload or catalog as a fatal bundle error.
- Report missing unreachable or unreferenced payload clearly and exclude the
  incomplete bundle from native installation until its behavior is proven.
- Keep OS, architecture, product type, and signature boundaries explicit.
- Never repair a package by borrowing a same-named binary from another driver
  version. Package signatures and file versions must remain coherent.
- Store validation errors and warnings with the import and surface them through
  the existing index API and driver-management UI.
- Propagate relevant import exclusions into `driverResolution.warnings`; a
  deployment must not report an empty warning list when a matching device was
  left unresolved because its local INF was rejected.

Validation must run automatically after upload/finalization and during an
explicit index rebuild. Operators should not need to inspect an INF tree before
using it.

## Phase 2: first-boot local reconciliation

Add a bounded reconciliation step before deployment completion:

1. Enumerate present devices, their ordered hardware and compatible IDs,
   status, and problem code in the installed Windows environment.
2. Submit the inventory to a deployment-scoped IronAPI endpoint.
3. Resolve only local, enabled, target-compatible bundles that were not already
   included in the offline snapshot.
4. Download a content-addressed archive authorized for that deployment.
5. Install it with the native Windows driver tooling and record the result.
6. Re-enumerate devices and repeat until no new devices or local candidates
   appear, with a default maximum of three passes.

The repeated pass is required because installing one parent driver can create
new `SWC`, `EXT`, audio, Bluetooth, or other child devices. The pass limit and
no-progress check prevent dependency cycles from blocking SetupComplete.

Reconciliation failure should be visible and retryable. Whether it blocks
deployment completion should be controlled by the selected driver policy.

## Phase 3: controlled Windows Update fallback

Windows must not race IronDeploy for drivers while local reconciliation is in
progress. Use a reversible, deployment-scoped policy to hold automatic driver
retrieval, then restore the configured steady-state policy after reconciliation.

Planned policies:

| Policy | Local passes | Windows Update fallback | Completion behavior |
| --- | --- | --- | --- |
| `local-strict` | Offline and first boot | Disabled | Warn or fail on unresolved hardware according to profile |
| `local-first` | Offline and first boot | Enabled after local passes | Complete after fallback observation window |
| `windows-managed` | Optional offline pass | Managed by existing Windows policy | Do not impose a temporary hold |

The fallback boundary must respect domain Group Policy and configured WSUS
behavior. IronDeploy should not permanently override an administrator's update
policy.

`AUTO_LOCAL_WSUS` remains a separate provider concern. Implementing it must not
be conflated with allowing a deployed client to contact Windows Update directly.

## Phase 4: provenance and reporting

Persist a per-device driver outcome rather than only aggregate candidate data:

- `local-offline`: selected by IronAPI and injected in WinPE;
- `local-firstboot`: selected by IronAPI during reconciliation;
- `inbox`: supplied by the deployed Windows image;
- `windows-update`: installed by the Windows update or device retrieval client;
- `unresolved`: no working driver after the configured passes;
- `superseded`: a previously bound package was replaced, including both old and
  new source/version details.

Each record should include the device instance ID, matched hardware ID, INF
published/original name, provider, version, package snapshot digest, timestamps,
result code, and deployment ID. Secrets, deployment tokens, certificate payloads,
and unrelated event-log data must not be included.

The deployment UI should show aggregate counts and allow expansion to device
details. Diagnostics should remain exportable for offline review, but routine
origin accounting must not depend on parsing `setupapi.dev.log` by hand.

## Phase 5: package lifecycle and observability

- Publish driver imports atomically as immutable snapshots; a deployment keeps
  using the snapshot it resolved even if an operator uploads a replacement.
- Record package health, index generation, validation time, INF count, usable
  bundle count, rejected bundle count, and warning count.
- Show which enabled packages cover a hardware ID and why a candidate was
  excluded.
- Support safe revalidation/reindexing without starting IronAPI implicitly or
  modifying source driver files.
- Retain bounded reconciliation and provenance records long enough to compare
  repeated deployments of the same model.
- Provide metrics for local coverage, Internet fallback rate, unresolved
  devices, superseded drivers, archive size, and reconciliation duration.

## Near-term alternative: full exact-model archive

As a temporary reliability mode, IronDeploy could inject every valid bundle
from one explicitly selected exact-model package. This would cover many child
devices that WinPE cannot inventory, but it increases transfer size, driver
store size, and exposure to conflicting or irrelevant vendor components.

This mode can be useful as an operator-selected fallback, but it does not
replace first-boot reconciliation and should not become the implicit behavior
of hardware-aware AUTO selection.

## Acceptance criteria

- An incomplete matching Wi-Fi bundle is withheld without causing the other
  valid bundles in the deployment archive to fail DISM.
- A missing file that is actually reachable through an applicable install path
  causes a clear import error before deployment.
- Child devices created after parent-driver installation receive matching local
  drivers before Internet fallback is released.
- The same package snapshot is used throughout one deployment.
- Windows Update cannot win the driver race during local-first reconciliation.
- The final report accounts for every present device and identifies every
  superseded package.
- Interrupted reconciliation is bounded, recoverable, and safe to retry.
- Tests cover parser reachability, child-device multi-pass behavior, no-progress
  termination, policy restoration, snapshot immutability, and provenance.

## Implementation boundaries

- `Core/Share/Drivers` remains the source of truth for uploaded driver files.
- `Core/Data/drivers_index.sqlite` remains disposable and rebuildable.
- Driver downloads remain deployment-scoped and authenticated.
- Post-install work must occur before the deployment token is invalidated or
  use a narrower reconciliation credential designed for that purpose.
- No roadmap item authorizes silently weakening certificate validation,
  signature checks, domain policy, or archive path validation.
- Changes to WinPE runtime files require the documented WinPE workflow and a
  separately requested ISO rebuild.
