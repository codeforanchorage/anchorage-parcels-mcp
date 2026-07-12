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

# Domain decision still open (e.g. anchorage-parcels.codeforanchorage.org).
# Leave empty to serve on the raw API Gateway URL; set it (plus ACM cert
# and DNS) and redeploy when decided.
custom_domain = ""

# Cap concurrent Lambda executions. Cost and blast-radius protection;
# conversational MCP traffic does not need horizontal scale.
lambda_reserved_concurrency = 10

# WAF per-IP rate limit (rolling 5-minute window). Same rationale as the
# GIS deployment: ~1 rps sustained per IP is plenty for real users.
waf_rate_limit_per_5min = 300

# No M365 GCC Copilot consumer for the parcels server; public /mcp only.
enable_gcc_route = false
