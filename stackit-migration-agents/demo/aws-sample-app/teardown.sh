#!/usr/bin/env bash
# Destroys the demo SOURCE environment created by this directory's Terraform.
# This is for the demo app only; the migration system itself never deletes data.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f terraform.tfstate && ! -d .terraform ]]; then
  echo "Nothing to tear down here (no Terraform state)." >&2
  exit 0
fi

terraform show -no-color | grep -E '^# (aws_instance|aws_db_instance|aws_s3_bucket)\.' || true
read -r -p "Destroy the demo source environment above? Type 'destroy' to confirm: " answer
if [[ "${answer}" != "destroy" ]]; then
  echo "Aborted." >&2
  exit 1
fi
terraform destroy -auto-approve -var "admin_cidr=${ADMIN_CIDR:-127.0.0.1/32}"
