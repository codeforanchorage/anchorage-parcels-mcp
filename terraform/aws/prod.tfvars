lambda_name = "anchorage-parcels-mcp-prod"
stage_name  = "prod"
aws_region  = "us-west-2"
config_file = "config.yaml"
# NOTE: lambda_memory and lambda_timeout here are OVERRIDDEN by the aws:
# block in config.yaml (see terraform/aws/main.tf locals) -- they are kept
# in sync so this file is not misleading, but config.yaml is the file to
# edit. lambda_name works the OPPOSITE way: this file wins.
#
# 512 MB / 28 s: the parcels tools are attribute queries and server-side
# statistics against a single Feature Layer -- no polygon-cache or
# point-in-polygon batch workloads like the GIS server's aggregate tools.
# 28 s sits just under API Gateway's hard, non-adjustable 29 s ceiling.
lambda_memory  = 512
lambda_timeout = 28

api_quota_limit = 3000
api_rate_limit  = 5
api_burst_limit = 10

# DNS lives in Dreamhost. Two CNAMEs required: the ACM validation record
# (from `terraform output acm_validation_cname_*`) and the traffic record
# pointing at `terraform output custom_domain_target`.
custom_domain = "anchorage-parcels.codeforanchorage.org"

# Cap concurrent Lambda executions. Cost and blast-radius protection;
# conversational MCP traffic does not need horizontal scale.
lambda_reserved_concurrency = 10

# WAF per-IP rate limit (rolling 5-minute window). Same rationale as the
# GIS deployment: ~1 rps sustained per IP is plenty for real users.
waf_rate_limit_per_5min = 300

# No M365 GCC Copilot consumer for the parcels server; public /mcp only.
enable_gcc_route = false

# Use the fleet-wide WAF instead of a dedicated ACL for this MCP. A dedicated
# ACL costs ~$8/mo in fixed AWS charges regardless of traffic; the shared ACL
# keeps this MCP's 300/5min limit as its own counter, aggregated on
# (IP, Host) so it stays independent of the other MCPs sharing that limit.
#
# The effective limit now lives in mcp-stats' `fleet_waf_members` under the key
# `anchorage-parcels` — change it there, not here. The rate-limit value above is retained
# so that rolling back (use_shared_waf = false) restores the original limit.
# See mcp-stats/docs/waf-consolidation.md.
use_shared_waf = true
