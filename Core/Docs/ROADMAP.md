# IronDeploy roadmap

This file contains planned work, not current behavior. Current behavior must be
documented in the owning component document.

## Local administrator safety

- Add a post-install guard before disabling the setup local administrator.
- Verify that the machine is domain-joined and that an alternative
  administrator path exists.
- Keep the setup local administrator enabled when domain join is disabled or
  fails, avoiding an operator lockout.
- Surface the resulting state clearly in SetupWeb before allowing an
  aggressive cleanup profile.
- Define how the built-in Administrator password is set or managed when that
  account is enabled.
