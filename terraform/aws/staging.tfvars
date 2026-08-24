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

# Use the fleet-wide WAF instead of a dedicated ACL, matching prod. A
# dedicated ACL costs ~$8/mo in fixed AWS charges ($5 per web ACL + $1 per
# rule x 3) regardless of traffic, which is hard to justify for staging.
#
# This is behaviour-preserving, not a downgrade. Staging has no custom
# domain, so its execute-api host is not one of the fleet ACL's
# Host-scoped members; it falls to the `rate-limit-unmatched-host` rule,
# which is per-IP at 300 per 5 minutes -- exactly the limit the dedicated
# ACL enforced. The same two AWS managed rule groups (KnownBadInputs,
# CommonRuleSet) apply under either ACL.
#
# waf_rate_limit_per_5min above is no longer read while this is true (see
# terraform/aws/waf.tf); it is retained so that rolling back to
# use_shared_waf = false restores the original limit.
use_shared_waf = true
