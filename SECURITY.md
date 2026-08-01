# Security Policy

## Reporting a vulnerability

Please do not report security issues in public Issues or Discussions.

Use GitHub's **private vulnerability reporting** on this repository:
the **Security** tab → **Report a vulnerability**. This creates a private
advisory visible only to the reporter and repository maintainers.

Expect a first response within a few days. IronDeploy is maintained by a single
author, so please allow reasonable time before disclosing publicly.

## Supported versions

Only the latest release is supported. Fixes are not backported.

## Scope

IronDeploy is on-premises infrastructure that runs with high privilege. The
following areas are the ones where a defect has real consequences, and reports
about them are especially valuable:

- **Offline Domain Join.** ODJ blobs contain computer-account secrets. They are
  created locally by IronAPI, served over HTTP(S), applied offline with DISM,
  and deleted after acknowledgement. Anything that causes a blob to persist, to
  leak, or to be served to the wrong client is in scope.
- **Disk handling.** The WinPE runtime wipes disk 0. Anything that causes it to
  run somewhere it should not, or to target the wrong disk, is in scope.
- **Authentication.** Browser sessions, WinPE deployment bearers, the
  single-use SetupWeb bootstrap token, and the allowed-client network policy.
- **The SMB share and file handling.** Path traversal or unintended file
  placement through image, driver, or program names.
- **Integrity of delivered payloads.** Installer hash verification before
  execution, and the contents of the deployment manifest.

## Out of scope

- Deployments where the operator has deliberately configured an insecure setup
  (for example running IronAPI as `LocalSystem` after confirming the warning,
  or granting the service identity more Active Directory rights than needed).
- Findings that require administrative access to the IronDeploy server, since
  that access is already sufficient to control every deployment.
- Running `Core\WinPE\Runtime\deploy.ps1` on a normal Windows host. This wipes
  disk 0 by design and is documented as unsupported.

## Operator guidance

Before operating IronDeploy, complete the
[required accounts and file access](README.md#5-configure-required-accounts-and-file-access)
setup and the [security must-haves](README.md#security-must-haves)
described in the README.
