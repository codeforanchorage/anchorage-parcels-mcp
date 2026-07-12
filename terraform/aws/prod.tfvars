lambda_name = "anchorage-parcels-mcp-prod"
stage_name  = "prod"
aws_region  = "us-west-2"
config_file = "config.yaml"
# 512 MB / 60 s: the parcels tools are attribute queries and server-side
# statistics against a single Feature Layer -- no polygon-cache or
# point-in-polygon batch workloads like the GIS server's aggregate tools.
lambda_memory  = 512
lambda_timeout = 60

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
