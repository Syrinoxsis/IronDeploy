# Security Policy

## Reporting a vulnerability

Please do not report security issues in public Issues or Discussions.

Use GitHub's **private vulnerability reporting** on this repository:
the **Security** tab → **Report a vulnerability**. This creates a private
advisory visible only to the reporter and repository maintainers.

Expect a first response within a few days. IronDeploy is maintained by a single
author, so please allow reasonable time before disclosing publicly.

## Supported versions

During Alpha, only the latest version on the default branch is supported. Fixes
are not backported.

## Scope

IronDeploy is on-premises infrastructure that runs with high privilege. The
following areas are the ones where a defect has real consequences, and reports
about them are especially valuable:

- **Offline Domain Join.** ODJ blobs contain computer-account secrets. They are
  created locally by IronAPI, served over HTTP(S), applied offline with DISM,
  and deleted after acknowledgement. Anything that causes a blob to persist, to
  leak, or to be served to the wrong client is in scope.
- **Disk handling.** The WinPE runtime erases the physical disk the operator
  selects. Anything that causes it to run where it should not, to erase a disk
  other than the selected one, or to misreport a disk's number, model, or size
  before the operator confirms, is in scope.
- **Authentication.** Browser sessions, WinPE deployment bearers, the
  single-use SetupWeb bootstrap token, and the allowed-client network policy.
- **The SMB share and file handling.** Path traversal or unintended file
  placement through image, driver, or program names.
- **Integrity of delivered payloads.** Installer hash verification before
  execution, and the contents of the deployment manifest.

## Out of scope

- Deployments where the operator has deliberately configured an insecure setup
  (for example granting the service identity more Active Directory rights than
  needed, or granting the WinPE SMB account write access).
- Findings that require administrative access to the IronDeploy server, since
  that access is already sufficient to control every deployment.
- Running `Core\WinPE\Runtime\deploy.ps1` on a normal Windows host. It erases a
  physical disk by design and is documented as unsupported.

## Operator guidance

Before operating IronDeploy, complete the
[account and file-access requirements](README.md#before-the-first-deployment)
and the [security essentials](README.md#security-essentials)
described in the README.
