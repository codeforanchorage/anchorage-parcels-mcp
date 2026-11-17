"""Invariants on the shipped deployment configuration.

Unlike the rest of the suite, these tests read the REAL files this fork
deploys (``terraform/aws/prod.tfvars``, ``terraform/aws/api_gateway.tf``)
rather than fixtures. They exist because the values below are only wrong at
runtime, in production, under load -- there is no unit under test that would
otherwise notice.

The timeout ladder (plugin < Lambda < API Gateway's 29s) is NOT covered
here: CI asserts it in its config-validation step.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROD_TFVARS = REPO_ROOT / "terraform" / "aws" / "prod.tfvars"
API_GATEWAY_TF = REPO_ROOT / "terraform" / "aws" / "api_gateway.tf"


def _tfvar_int(name: str) -> int:
    """Read a bare integer variable out of prod.tfvars."""
    text = PROD_TFVARS.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\s*=\s*(\d+)\s*$", text, re.MULTILINE)
    assert match, f"{name} not found in {PROD_TFVARS}"
    return int(match.group(1))


class TestCapacity:
    """Throttle and concurrency settings that only bite under real load."""

    def test_stage_throttle_reads_the_tfvars(self):
        """The stage-wide method settings must use the tfvars, not literals.

        They are the ONLY throttle on the keyless public /mcp route (the
        usage plan applies to API-key requests). They were once hard-coded
        to 10/5, so raising api_*_limit in prod.tfvars would have silently
        changed nothing for real users.
        """
        text = API_GATEWAY_TF.read_text(encoding="utf-8")
        block = re.search(
            r'resource "aws_api_gateway_method_settings" "mcp_post" \{.*?\n\}',
            text,
            re.DOTALL,
        )
        assert block, "aws_api_gateway_method_settings.mcp_post not found"
        body = block.group(0)
        assert re.search(r"throttling_burst_limit\s*=\s*var\.api_burst_limit", body)
        assert re.search(r"throttling_rate_limit\s*=\s*var\.api_rate_limit", body)

    def test_burst_at_least_rate(self):
        assert _tfvar_int("api_burst_limit") >= _tfvar_int("api_rate_limit")

    def test_reserved_concurrency_covers_burst(self):
        """Every request the gateway admits in a burst should find a Lambda
        slot; otherwise the overflow surfaces as Lambda throttles (502s)
        rather than clean gateway 429s."""
        assert _tfvar_int("lambda_reserved_concurrency") >= _tfvar_int(
            "api_burst_limit"
        )
