###############################################################################
# CloudWatch alarms for the MCP server.
#
# Surfaces Lambda errors/throttles, API Gateway 5XX spikes, 4XX probing
# patterns, and Lambda duration approaching the configured timeout.
#
# The alarms exist only when `alarm_sns_topic_arn` is set. An alarm with no
# action notifies nobody and still costs $0.10 a month; the mcp-stats fleet
# probe and POST /mcp 4xx alarms cover every server either way.
###############################################################################

locals {
  alarm_actions  = var.alarm_sns_topic_arn != "" ? [var.alarm_sns_topic_arn] : []
  alarms_enabled = var.alarm_sns_topic_arn != ""
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  count = local.alarms_enabled ? 1 : 0

  alarm_name          = "${local.lambda_name}-lambda-errors"
  alarm_description   = "Lambda function is returning errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.mcp_server.function_name
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "lambda_throttles" {
  count = local.alarms_enabled ? 1 : 0

  alarm_name          = "${local.lambda_name}-lambda-throttles"
  alarm_description   = "Lambda function is being throttled (concurrency limit hit)"
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.mcp_server.function_name
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "lambda_duration_near_timeout" {
  count = local.alarms_enabled ? 1 : 0

  alarm_name          = "${local.lambda_name}-lambda-duration-near-timeout"
  alarm_description   = "Lambda p95 duration is approaching the configured timeout"
  namespace           = "AWS/Lambda"
  metric_name         = "Duration"
  extended_statistic  = "p95"
  period              = 300
  evaluation_periods  = 2
  threshold           = local.lambda_timeout * 1000 * 0.8
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.mcp_server.function_name
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "apigw_5xx" {
  count = local.alarms_enabled ? 1 : 0

  alarm_name          = "${local.lambda_name}-apigw-5xx"
  alarm_description   = "API Gateway is returning 5XX responses"
  namespace           = "AWS/ApiGateway"
  metric_name         = "5XXError"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    ApiName = aws_api_gateway_rest_api.mcp_api.name
    Stage   = aws_api_gateway_stage.prod.stage_name
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}
