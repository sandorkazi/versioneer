---
description: Safe versioneer deploy — dry-run preview first, apply only on approval
---

For versioneer config $ARGUMENTS: 1) run versioneer_deploy_preview and show the full preview,
2) wait for explicit approval (especially for /etc paths — remind about .bak and live USB for fstab),
3) only then run versioneer_deploy_apply with confirm=true.
Manifest replays stay print-only unless the user asks for apply=true.
