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
# These two feed BOTH the usage plan and the stage-wide method settings
# (api_gateway.tf), so they are the effective throttle on the keyless public
# /mcp route. Raised 5/10 -> 15/30 on 2026-11-17 to match the eCode server
# ahead of ~12 attorneys onboarding via Claude.ai: their workflow drives
# eCode, GIS and Parcels in the same minutes.
api_rate_limit  = 15
api_burst_limit = 30

# DNS lives in Dreamhost. Two CNAMEs required: the ACM validation record
# (from `terraform output acm_validation_cname_*`) and the traffic record
# pointing at `terraform output custom_domain_target`.
custom_domain = "anchorage-parcels.codeforanchorage.org"

# Cap concurrent Lambda executions. Cost and blast-radius protection if WAF
# is bypassed via distributed sources. Raised 10 -> 30 on 2026-11-17 for ~12
# attorneys onboarding via Claude.ai (30-day peak before: 4 concurrent; the
# GIS server hit its own cap of 10 on 2026-11-10 and throttled). The Lambda
# Throttles alarm fires on any throttle at this cap.
lambda_reserved_concurrency = 30

# WAF per-IP rate limit (rolling 5-minute window). Same rationale as the
# GIS deployment: ~1 rps sustained per IP is plenty for real users.
# Raised 300 -> 2000 on 2026-11-17: Claude.ai connector traffic arrives from
# ~25 shared Anthropic egress IPs, so many users share one per-IP counter.
# Not read while use_shared_waf = true (see below); kept equal to the limit
# mcp-stats sets in fleet_waf_members so a rollback does not tighten it.
waf_rate_limit_per_5min = 2000

# No M365 GCC Copilot consumer for the parcels server; public /mcp only.
enable_gcc_route = false

# Use the fleet-wide WAF instead of a dedicated ACL for this MCP. A dedicated
# ACL costs ~$8/mo in fixed AWS charges regardless of traffic; the shared ACL
# keeps this MCP's 2000/5min limit as its own counter, aggregated on
# (IP, Host) so it stays independent of the other MCPs sharing that limit.
#
# The effective limit now lives in mcp-stats' `fleet_waf_members` under the key
# `anchorage-parcels` — change it there, not here. The rate-limit value above is retained
# so that rolling back (use_shared_waf = false) keeps the same limit.
# See mcp-stats/docs/waf-consolidation.md.
use_shared_waf = true
