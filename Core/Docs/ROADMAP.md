# Roadmap

This is the intended direction of IronDeploy, not a schedule or a commitment.
IronDeploy is built by a single author, so the order below changes whenever
real deployments show that something else matters more. Hardware reports from
other people are the main thing that moves items up this list.

IronDeploy currently installs Windows 10 and Windows 11. Support for other
systems is not ruled out, but it is not what the current runtime is built for.

## Disk handling

The operator now selects the target disk in WinPE after reviewing its number,
model, and size. IronAPI records that snapshot with the deployment.

- Show the selected disk's existing partitions in the WinPE interface before
  the erase is confirmed.
- Support BIOS/MBR systems in addition to x64 UEFI/GPT.

## Deployment coverage

- Grow a list of models that are known to deploy cleanly, built from reports
  rather than from claims.
- Validate the generated ISO with Ventoy, which is currently untested.

## Active Directory

- Encrypt Offline Domain Join blobs at rest. They currently rely on NTFS
  permissions and a short lifetime.
- Report whether Offline Domain Join is configured to the WinPE interface, so
  it stops offering a domain join that IronAPI will refuse.

## Post-install automation

- Decide the retention period, total storage quota, and cleanup policy for raw
  post-PowerShell output stored by IronAPI.

## Project infrastructure

- Run the test suite automatically on Windows, where the WinPE and service
  tests can actually execute.
- Add issue templates that ask for the details a hardware report needs.

## Outside the current scope

- Capturing or cloning an already configured reference computer. IronDeploy
  deploys from a Windows installation image by design.
- Managing computers after deployment. Updates, policies, inventory, and
  software delivery belong to a configuration-management product, and are not
  on the near-term path for IronDeploy.
