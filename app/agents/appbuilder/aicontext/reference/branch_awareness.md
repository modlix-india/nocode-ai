---
name: branch-awareness
description: Each environment runs a specific branch of nocode-saas; the agent must align its source reads to the env's branch.
metadata:
  type: reference
---

# Branches per environment

| Env label (`MODLIX_ENV_NAME`) | nocode-saas source |
|---|---|
| `local` | The `nocode-saas` checkout in the CFA workspace |
| `dev` | branch `oci-development` |
| `stage` | branch `oci-stage` |
| `prod` | branch `oci-production` |

Source repo: **https://github.com/modlix-india/nocode-saas**

The local working tree may be checked out to ANY branch at a given moment;
direct `Read` of files returns whatever's checked out. Before drawing
source-grounded conclusions about a non-local env, **align with the env's
branch**.

# Reading source for a specific env without changing the working tree

```bash
cd "$CFA_WORKSPACE_DIR/nocode-saas"     # default /var/cfa/workspace
git fetch origin
# Show a file at a specific branch without checkout
git show origin/oci-development:security/src/main/java/com/fincity/security/controller/UserController.java
# Or list changes between two branches
git log --oneline master..origin/oci-stage
# Or diff a specific file
git diff master origin/oci-production -- security/src/main/java/.../UserController.java
```

For deeper exploration, **check out** the matching branch in a worktree
(non-destructive — leaves your main checkout alone):

```bash
cd "$CFA_WORKSPACE_DIR/nocode-saas"
git worktree add ../nocode-saas-oci-stage origin/oci-stage
# the sibling nocode-saas-oci-stage now holds the stage source
```

# Configuration repo

The `oci-config` repo holds env-specific
configuration (URLs, secrets, feature flags). If a behavior differs across
envs and the code looks identical, the answer is usually in `oci-config`
overrides for that env.

# What this means for tools

The CFA ships read-only `code_workspace` tools (`code_read`, `code_grep`,
`code_glob`, `code_ls`, `code_list_repos`) that target the bundled
shallow checkouts of `nocode-saas` / `nocode-ui` / `nocode-kirun` on the
agent host. These checkouts track `master` and refresh after each deploy
via `/api/ai/admin/code-workspace/pull` — they're NOT auto-aligned with
the target env's branch. When quoting source as authoritative, pair
`which_environment` with `code_list_repos` (which reports each repo's
current SHA + last-fetched timestamp) and confirm the SHA matches what
that env actually runs.
