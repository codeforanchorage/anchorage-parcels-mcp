# Deployment

The server runs on AWS Lambda (Python 3.14) behind a REST API Gateway,
in us-west-2, deployed with Terraform by `scripts/deploy.sh`.

| Environment | Terraform workspace | Lambda | URL |
| ----------- | ------------------- | ------ | --- |
| staging | `anchorage-parcels-staging` | `anchorage-parcels-mcp-staging` | the `api_gateway_url` output |
| prod | `anchorage-parcels-prod` | `anchorage-parcels-mcp-prod` | `https://anchorage-parcels.codeforanchorage.org/mcp` |

## Prerequisites

- AWS CLI with credentials for the account
- Terraform 1.0 or later
- Python 3.11 or later, and Bash (Git Bash on Windows)
- The Terraform state backend, created once: see
  [`terraform/bootstrap/README.md`](../terraform/bootstrap/README.md)

## Deploy

```bash
cp config-anchorage-parcels.yaml config.yaml   # config.yaml is what gets packaged
bash ./scripts/deploy.sh --environment staging --yes
python scripts/smoke_http.py staging           # 56 HTTP-level checks
```

Production is the same with `prod`. Deploy to staging and run the
smoke test before deploying to prod. On Windows prefix the deploy with
`PYTHONUTF8=1`.

The script validates the config, builds the Lambda package (this
repository's `plugins/` plus the pinned dependencies), selects the
workspace, runs `terraform plan` and `apply`, and checks that the
config inside the package matches the source.

| Flag | Meaning |
| ---- | ------- |
| `--environment`, `-e` | `staging` or `prod` (required) |
| `--tfworkspace`, `-w` | Workspace name, if not the default above |
| `--yes`, `-y` | Skip the interactive confirmation before apply |
| `--allow-dirty` | Package a working tree with uncommitted changes. Emergencies only; the build is stamped as dirty |

The script leaves Terraform on the workspace it last deployed. Check
`terraform workspace show` in `terraform/aws/` before running any
Terraform command by hand.

## Where a setting lives

This is easy to get wrong, and a mistake fails silently.

| Setting | Edit it in | Notes |
| ------- | ---------- | ----- |
| Plugin settings (layer URLs, `timeout`), server `instructions` | `config.yaml` (and its tracked source) | Shipped inside the zip. Never appears as a Terraform diff |
| `lambda_timeout`, `lambda_memory` | The `aws:` block of `config.yaml` | Overrides the values in `terraform/aws/*.tfvars` |
| `lambda_name`, stage, region, throttles, concurrency, custom domain | `terraform/aws/staging.tfvars` / `prod.tfvars` | Here the tfvars file wins |
| WAF per-IP rate limit | The mcp-stats repository (`fleet_waf_members`) | Both environments use the shared fleet ACL |

`terraform/aws/config.yaml` and `terraform/aws/lambda-deployment.zip`
are build output. `deploy.sh` overwrites them, so editing them does
nothing, and a `terraform plan` run without repackaging reads the stale
copies.

The config is read from the package (`$LAMBDA_TASK_ROOT/config.yaml`),
not from the `OPENCONTEXT_CONFIG` environment variable, which Terraform
sets to the empty string: the file is larger than the 4 KB AWS allows
for environment variables. It does not support `${ENV_VAR}`
substitution, and nothing secret belongs in it.

### Timeouts

Keep this order: API Gateway's fixed 29 s > `aws.lambda_timeout` 28 s >
the plugin's `timeout` 20 s. A plugin timeout above the Lambda's means
a slow upstream kills the Lambda mid-request, and the caller gets a
gateway error where a readable tool error was possible. CI checks the
order.

## Limits in force

| | staging | prod |
| - | ------- | ---- |
| API Gateway throttle (requests/s, burst) | 5, 10 | 15, 30 |
| Lambda reserved concurrency | 5 | 30 |
| WAF per-IP limit per 5 minutes | 300 | 2000 |
| Lambda memory, timeout | 512 MB, 28 s | 512 MB, 28 s |

A throttled request gets HTTP 429. [SECURITY.md](SECURITY.md) explains
what each limit is for.

## Outputs and logs

```bash
cd terraform/aws
terraform output -raw api_gateway_url     # includes /mcp
terraform output cloudwatch_log_group
```

- Lambda logs: `/aws/lambda/<lambda name>`, kept 14 days.
  `aws logs tail /aws/lambda/anchorage-parcels-mcp-prod --follow`
- API Gateway access logs: the `api_gateway_access_log_group` output,
  kept 30 days.
- CloudWatch alarms are created only when `alarm_sns_topic_arn` is set.

On Git Bash set `MSYS_NO_PATHCONV=1` for AWS CLI commands that take a
log group name, or the leading `/` is rewritten as a Windows path.

### Custom domain

Prod sets `custom_domain` in `prod.tfvars`. DNS is managed outside AWS
and needs two CNAME records: the certificate validation record
(`acm_validation_cname_name` / `acm_validation_cname_value` outputs)
and the traffic record pointing at the `custom_domain_target` output.

## Deploying a fork

Before the first deploy, give the fork its own `lambda_name` in both
tfvars files and its own Terraform state bucket and workspaces.
Otherwise the first apply will try to replace this deployment's
Lambda. See "Fork this for your city" in [PARCELS.md](PARCELS.md) for
the plugin side.

## Troubleshooting

| Symptom | Look at |
| ------- | ------- |
| Deploy stops at "Multiple Plugins Enabled" or a config error | `config.yaml`: exactly one plugin enabled |
| Deploy refuses a dirty tree | Commit first; `--allow-dirty` only in an emergency |
| A config change did not take effect | It was edited in `terraform/aws/config.yaml`, or deployed without `deploy.sh` |
| HTTP 403 before any tool runs | The request's `Origin` is not on the allowlist. Requests with no `Origin` are allowed |
| HTTP 429 | A throttle or the WAF limit above |
| HTTP 504, or a call cut off near 29 s | The timeout order above |
| Tool errors naming a quota or 429 from ArcGIS | The upstream's per-IP limit. Wait and retry before blaming the deploy |
