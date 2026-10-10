# STACKIT service account and roles

The migration agents act on STACKIT through **one service account that is
scoped to one project**: the target project of the migration. Nothing in
this system needs organisation- or folder-wide permissions.

> Sources: STACKIT Terraform provider docs and source (v0.119.0) and the
> STACKIT CLI docs. `docs.stackit.cloud` could not be reached while this was
> written; statements that rest only on search snippets are marked
> **TODO(verify)**.

## Role model

- Current basic project roles: **`owner`**, **`editor`**, **`reader`** (no
  `project.` prefix). The legacy names `project.owner`, `project.admin`,
  `project.member` and `project.auditor` are rejected by the provider
  ("legacy roles are not supported").
- Product roles follow `<product>.<noun>` (for example `postgres-flex.reader`).
  **TODO(verify):** the exact per-service role names. List them for your
  project with:

  ```bash
  stackit project role list --project-id <PROJECT_ID> --output-format json
  ```

- Role bindings are hierarchical: a role granted on a folder or the
  organisation also applies to every project below it. **Grant roles on the
  target project only.**

## What each part of the system needs

| Component | STACKIT access | Role on the target project |
|---|---|---|
| Discovery: catalogue lookups (`stackit server machine-type list`, `postgresflex flavor/version list`, `mariadb plans`, `ske options`) | read | `reader` |
| Deployment, `apply` mode (`terraform plan` / `apply`) | create servers, networks, security groups, volumes, public IPs, PostgreSQL Flex, MariaDB, Object Storage, load balancers, SKE | `editor` (see note) |
| Deployment, `pr` mode | none: the client's own pipeline applies | (pipeline's account) |
| Data migration | Object Storage credentials (from the Terraform outputs) and the database users created by Terraform | no IAM role; data-plane credentials only |
| Monitoring | Observability query credential (`stackit_observability_credential`, basic auth) | no IAM role; data-plane credential only |

**Note on `editor`:** editor can create and delete resources for most
services but cannot assign roles (TODO(verify): confirmed for every service
above only by documentation snippets). The generated Terraform does not
create role assignments, so `owner` is **not** needed. If a service rejects
`editor`, add the specific product role instead of upgrading to `owner`, or
define a custom role (`stackit_authorization_project_custom_role`) from the
permission list above.

Separate accounts per stage are recommended for production engagements:
a `reader` account for discovery and an `editor` account that only the
approved deployment step uses.

## Setup

```bash
# 1. Service account inside the target project
stackit service-account create --name migration-agents --project-id <PROJECT_ID>

# 2. Grant roles on THIS project only (subject = the service account e-mail)
stackit project member add <SA_EMAIL> --project-id <PROJECT_ID> --role editor

# 3. Key for the key flow (keep the file outside the repository). Without
#    --public-key the response contains the private key, shown only once.
stackit service-account key create --email <SA_EMAIL> --project-id <PROJECT_ID> \
  --expires-in-days 30 --output-format json > ~/.stackit/migration-agents-key.json
chmod 600 ~/.stackit/migration-agents-key.json
```

TODO(verify): that the JSON response can be used as-is as the key file; the
documented alternative is to create and download the key in the STACKIT
portal.

Then in `.env`:

```bash
STACKIT_PROJECT_ID=<PROJECT_ID>
STACKIT_SERVICE_ACCOUNT_KEY_PATH=~/.stackit/migration-agents-key.json
```

The stackit MCP server exchanges the key for a short-lived access token with
`stackit auth activate-service-account --only-print-access-token`, so no
credentials are stored by the CLI, and the Terraform provider reads the same
key path from the environment.

In CI (PR mode), prefer **workload identity federation** over long-lived keys:
the provider supports `use_oidc = true` with `service_account_email`.

Role assignments can also be managed with Terraform
(`stackit_authorization_project_role_assignment`), but that resource is part
of the provider's experimental `iam` feature (`experiments = ["iam"]`), so
this project does not generate it.

## What the accounts can never do

- The AWS credentials are read-only (see `aws-readonly-policy.json`).
- The STACKIT account has no rights outside the target project.
- Mutating MCP tools (`terraform_apply`, `open_pull_request`, `cutover`)
  additionally refuse to run without an approval token issued after a human
  approved the exact plan file or verification report.
