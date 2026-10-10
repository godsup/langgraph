# Demo source app on AWS

A small 3-tier application to migrate in **live** demos (mock mode needs none
of this): 1 EC2 web server (nginx), 1 RDS PostgreSQL, 1 S3 bucket with
sample objects, in `eu-central-1`. It is deliberately tiny and cheap, but it
costs money while it runs: tear it down after the demo.

## Create

```bash
cd demo/aws-sample-app
terraform init
terraform apply -var "admin_cidr=$(curl -s https://checkip.amazonaws.com)/32"
```

SSH and PostgreSQL are reachable only from `admin_cidr`. The database master
password is kept in AWS Secrets Manager (`manage_master_user_password`), so it
never appears in the Terraform state or in this repository.

## Seed the database

```bash
SECRET_ARN=$(terraform output -raw db_master_secret_arn)
export PGPASSWORD=$(aws secretsmanager get-secret-value --secret-id "$SECRET_ARN" \
  --query SecretString --output text | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])')
psql "host=$(terraform output -raw db_endpoint) dbname=shop user=shopadmin sslmode=require" -f seed.sql
unset PGPASSWORD
```

## Migrate it

1. Create a **read-only** IAM user or role with `../../docs/aws-readonly-policy.json`
   and export its credentials (or set `AWS_PROFILE`).
2. Run discovery against the real account:

   ```bash
   uv run migrate discover --source aws --region eu-central-1
   ```

3. Continue with `migrate plan`, `migrate deploy --execute`, `migrate data
   --execute` as described in the main README. For the data step, set
   `PG_DSN_SOURCE_SMD_DB` / `PG_DSN_TARGET_SMD_DB` and the STACKIT Object
   Storage keys in `.env`.

Expected discovery findings: the RDS instance is publicly accessible (by
design of this demo), which the discovery report flags.

## Tear down

```bash
ADMIN_CIDR=<same value as above> ./teardown.sh
```

This destroys the **demo source environment only**. The migration system
itself never deletes source data.
