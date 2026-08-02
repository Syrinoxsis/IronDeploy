# Contributing to IronDeploy

IronDeploy is developed and maintained by a single author. That shapes what is
useful to send and what is not.

## What is very welcome

**Bug reports and deployment results.** IronDeploy touches WinPE, DISM, Active
Directory, and real hardware, so the failure modes that matter are the ones
found on machines the author does not have. A report of what broke on your
hardware is the most valuable thing you can send.

Useful reports include:

- the machine model, and the manufacturer and system SKU if known;
- the deployment stage that failed, as shown in the WinPE progress screen;
- the relevant part of the WinPE log or `Core\Logs\IronAPI-service.log`;
- whether domain join was enabled for that deployment;
- the driver package selected, or that **Do not install drivers** was chosen.

Please open a **GitHub Issue** for these.

**Hardware compatibility notes.** Reports that a given model deploys cleanly are
useful too, not only failures.

**Questions and feature ideas.** Please use **GitHub Discussions** rather than
Issues, so the issue tracker stays a list of things that are actually broken.

## Pull requests

**Pull requests are not accepted.** This is a deliberate decision, not an
oversight, and it is not a judgement about the quality of the code you would
send.

IronDeploy erases a selected physical disk, applies Windows images offline, and
handles Offline Domain Join blobs that contain computer-account secrets. A
change to that path cannot be validated by reading it. It needs the Windows
ADK, a domain, and a test machine — which means the author has to reproduce and
verify the change regardless of who wrote it. Reviewing a patch to this code
costs more than writing it, so accepting patches would slow the project down
rather than speed it up.

Please open an Issue describing the problem instead. A precise description of
what is wrong is worth more here than a diff.

## Security issues

Do not report security issues in public Issues or Discussions. See
[SECURITY.md](SECURITY.md).

## Commercial use

IronDeploy is released under the Apache License, Version 2.0, and may be used
commercially at no cost. Deployment, integration, support, and custom development may be available as
paid services. Start a [GitHub Discussion](https://github.com/Syrinoxsis/IronDeploy/discussions)
for general and commercial inquiries.
