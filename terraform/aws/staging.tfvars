lambda_name = "anchorage-parcels-mcp-staging"
stage_name  = "staging"
aws_region  = "us-west-2"
config_file = "config.yaml"
lambda_memory  = 512
lambda_timeout = 60

api_quota_limit = 1000
api_rate_limit  = 5
api_burst_limit = 10

custom_domain = ""

lambda_reserved_concurrency = 5
waf_rate_limit_per_5min     = 300
enable_gcc_route            = false
