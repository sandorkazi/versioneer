---
description: Guided versioneer commit — review drift, then save with a message
---

For versioneer config $ARGUMENTS: 1) run versioneer_status + versioneer_diff and show them,
2) ask which targets to save and for a commit message,
3) only then run versioneer_commit (targets or all=true) followed by versioneer_push.
Never commit blind.
