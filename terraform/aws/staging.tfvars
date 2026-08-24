lambda_name = "anchorage-parcels-mcp-staging"
stage_name  = "staging"
aws_region  = "us-west-2"
config_file = "config.yaml"
# NOTE: lambda_memory and lambda_timeout here are OVERRIDDEN by the aws:
# block in config.yaml (see terraform/aws/main.tf locals) -- they are kept
# in sync so this file is not misleading, but config.yaml is the file to
# edit. lambda_name works the OPPOSITE way: this file wins.
lambda_memory  = 512
lambda_timeout = 28

api_quota_limit = 1000
api_rate_limit  = 5
api_burst_limit = 10

custom_domain = ""

lambda_reserved_concurrency = 5
waf_rate_limit_per_5min     = 300
enable_gcc_route            = false
