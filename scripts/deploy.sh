#!/bin/bash
# OpenContext Deployment Script
#
# This script validates configuration and deploys the MCP server to AWS Lambda.
# It enforces the "one fork = one MCP server" rule.

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Script directory (scripts/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Project root (parent directory)
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# Parse named arguments
ENVIRONMENT=""
TF_WORKSPACE=""
ALLOW_DIRTY=0
AUTO_APPROVE="false"

show_usage() {
    echo "Usage: $0 --environment <staging|prod> [--tfworkspace <name>] [--yes] [--allow-dirty]"
    echo ""
    echo "Options:"
    echo "  --environment, -e   Deployment environment: staging or prod (required)"
    echo "  --tfworkspace, -w   Terraform workspace name (default: anchorage-parcels-staging or anchorage-parcels-prod)"
    echo "  --yes, -y           Skip the interactive apply confirmation (non-interactive runs)"
    echo "  --allow-dirty       Package the working tree even with uncommitted changes"
    echo "                      (emergencies only; the build is stamped as dirty)"
    echo "  --help, -h          Show this help message"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --environment|-e)
            ENVIRONMENT="$2"
            shift 2
            ;;
        --tfworkspace|-w)
            TF_WORKSPACE="$2"
            shift 2
            ;;
        --yes|-y)
            AUTO_APPROVE="true"
            shift
            ;;
        --allow-dirty)
            ALLOW_DIRTY=1
            shift
            ;;
        --help|-h)
            show_usage
            exit 0
            ;;
        *)
            echo -e "${RED}❌ Error: Unknown argument '${1}'${NC}"
            show_usage
            exit 1
            ;;
    esac
done

if [ -z "$ENVIRONMENT" ]; then
    echo -e "${RED}❌ Error: --environment is required${NC}"
    show_usage
    exit 1
fi

if [ "$ENVIRONMENT" != "staging" ] && [ "$ENVIRONMENT" != "prod" ]; then
    echo -e "${RED}❌ Error: Invalid environment '${ENVIRONMENT}'. Must be 'staging' or 'prod'.${NC}"
    show_usage
    exit 1
fi

# Default workspace per environment when not explicitly provided.
# NOTE: this fork deploys the PARCELS server. The GIS server lives in the
# anchorage-gis-* workspaces of the same state bucket -- never point this
# script at those or terraform will plan to rename the GIS Lambda.
if [ -z "$TF_WORKSPACE" ]; then
    if [ "$ENVIRONMENT" = "prod" ]; then
        TF_WORKSPACE="anchorage-parcels-prod"
    else
        TF_WORKSPACE="anchorage-parcels-staging"
    fi
fi

echo -e "${GREEN}🚀 OpenContext Deployment [${ENVIRONMENT}] (workspace: ${TF_WORKSPACE})${NC}"
echo "================================"
echo ""

# Check if config.yaml exists
if [ ! -f "config.yaml" ]; then
    echo -e "${RED}❌ Error: config.yaml not found${NC}"
    echo "Create config from template: cp config-example.yaml config.yaml"
    echo "Then edit config.yaml and enable exactly ONE plugin."
    exit 1
fi

# Check if Python is available (python3 on Linux/macOS, python on Windows)
if command -v python3 &> /dev/null; then
    PYTHON=python3
elif command -v python &> /dev/null; then
    PYTHON=python
else
    echo -e "${RED}❌ Error: python not found${NC}"
    echo "Please install Python 3.11 or later."
    exit 1
fi

# Check if Terraform is available
if ! command -v terraform &> /dev/null; then
    echo -e "${RED}❌ Error: terraform not found${NC}"
    echo "Please install Terraform: https://www.terraform.io/downloads"
    exit 1
fi

# The package is built from the files on disk, so what is on disk must be
# a commit: otherwise an uncommitted edit (possibly another session's,
# half finished) goes to AWS and the live code matches nothing in git.
# Untracked files count; ignored files do not.
GIT_COMMIT=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
GIT_DIRTY=$(git status --porcelain 2>/dev/null || true)
if [ -n "$GIT_DIRTY" ]; then
    if [ "$ALLOW_DIRTY" -ne 1 ]; then
        echo -e "${RED}❌ Error: the working tree has uncommitted changes:${NC}"
        echo "$GIT_DIRTY" | head -20
        echo ""
        echo "Commit or stash them, then deploy. To package the tree as it is"
        echo "(emergencies only), re-run with --allow-dirty."
        exit 1
    fi
    echo -e "${YELLOW}⚠️  --allow-dirty: packaging uncommitted changes${NC}"
fi

echo -e "${YELLOW}📋 Step 1: Validating configuration...${NC}"

# Count enabled plugins using Python for reliable YAML parsing
ENABLED_COUNT=$($PYTHON << 'EOF'
import yaml
import sys

try:
    with open('config.yaml', 'r') as f:
        config = yaml.safe_load(f)

    plugins = config.get('plugins', {})
    enabled = []

    for plugin_name, plugin_config in plugins.items():
        if isinstance(plugin_config, dict) and plugin_config.get('enabled', False):
            enabled.append(plugin_name)

    count = len(enabled)

    if count == 0:
        print("0", file=sys.stderr)
        print("No plugins enabled", file=sys.stderr)
        sys.exit(1)
    elif count > 1:
        print(str(count), file=sys.stderr)
        print(" ".join(enabled), file=sys.stderr)
        sys.exit(2)
    else:
        print(count)
        print(enabled[0], file=sys.stderr)
        sys.exit(0)

except Exception as e:
    print(f"Error parsing config.yaml: {e}", file=sys.stderr)
    sys.exit(3)
EOF
)

EXIT_CODE=$?

if [ $EXIT_CODE -eq 1 ]; then
    echo -e "${RED}❌ Configuration Error: No Plugins Enabled${NC}"
    echo ""
    echo "You must enable exactly ONE plugin in config.yaml."
    echo ""
    echo "To enable a plugin, set 'enabled: true' for:"
    echo "  • anchorage_parcels"
    echo "  • A custom plugin in custom_plugins/"
    echo ""
    echo "See docs/GETTING_STARTED.md for setup instructions."
    exit 1
elif [ $EXIT_CODE -eq 2 ]; then
    ENABLED_PLUGINS=$($PYTHON << 'EOF'
import yaml
with open('config.yaml', 'r') as f:
    config = yaml.safe_load(f)
plugins = config.get('plugins', {})
enabled = [name for name, cfg in plugins.items()
           if isinstance(cfg, dict) and cfg.get('enabled', False)]
print("\n".join(f"  • {name}" for name in enabled))
EOF
    )

    echo -e "${RED}❌ Configuration Error: Multiple Plugins Enabled${NC}"
    echo ""
    echo "You have $ENABLED_COUNT plugins enabled in config.yaml:"
    echo "$ENABLED_PLUGINS"
    echo ""
    echo "OpenContext enforces: One Fork = One MCP Server"
    echo ""
    echo "This keeps deployments:"
    echo "  ✓ Simple and focused"
    echo "  ✓ Independently scalable"
    echo "  ✓ Easy to maintain"
    echo ""
    echo "To deploy multiple MCP servers:"
    echo ""
    echo "  1. Fork this repository again"
    echo "     Example: opencontext-opendata, opencontext-mbta"
    echo ""
    echo "  2. Configure ONE plugin per fork"
    FIRST_PLUGIN=$(echo "$ENABLED_PLUGINS" | head -n1 | sed 's/  • //')
    SECOND_PLUGIN=$(echo "$ENABLED_PLUGINS" | tail -n1 | sed 's/  • //')
    echo "     Fork #1: Enable $FIRST_PLUGIN only"
    echo "     Fork #2: Enable $SECOND_PLUGIN only"
    echo ""
    echo "  3. Deploy each fork separately"
    echo "     ./scripts/deploy.sh (in each fork)"
    echo ""
    echo "See docs/ARCHITECTURE.md for details."
    exit 1
elif [ $EXIT_CODE -ne 0 ]; then
    echo -e "${RED}❌ Error validating configuration${NC}"
    exit 1
fi

ENABLED_PLUGIN=$($PYTHON << 'EOF'
import yaml
with open('config.yaml', 'r') as f:
    config = yaml.safe_load(f)
plugins = config.get('plugins', {})
enabled = [name for name, cfg in plugins.items()
           if isinstance(cfg, dict) and cfg.get('enabled', False)]
print(enabled[0])
EOF
)

echo -e "${GREEN}✓ Configuration valid: ${ENABLED_PLUGIN} plugin enabled${NC}"
echo ""

# Extract server name and AWS settings
SERVER_NAME=$($PYTHON << 'EOF'
import yaml
with open('config.yaml', 'r') as f:
    config = yaml.safe_load(f)
print(config.get('server_name', 'my-mcp-server'))
EOF
)

AWS_REGION=$($PYTHON << 'EOF'
import yaml
with open('config.yaml', 'r') as f:
    config = yaml.safe_load(f)
print(config.get('aws', {}).get('region', 'us-east-1'))
EOF
)

TFVARS_FILE="terraform/aws/${ENVIRONMENT}.tfvars"
LAMBDA_NAME=$(grep '^lambda_name' "$TFVARS_FILE" | sed 's/.*=\s*"\(.*\)"/\1/')

echo -e "${YELLOW}📦 Step 2: Packaging Lambda code...${NC}"

# Create deployment package directory
PACKAGE_DIR=".deploy"
rm -rf "$PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR"

# Copy this deployment's code to the package directory. core/ and server/
# are NOT copied: they come from the opencontext-core requirement, which the
# install step below places next to plugins/ in the package root.
cp -r plugins "$PACKAGE_DIR/"
cp -r custom_plugins "$PACKAGE_DIR/" 2>/dev/null || mkdir -p "$PACKAGE_DIR/custom_plugins"
cp requirements.txt "$PACKAGE_DIR/" 2>/dev/null || true
# Ship config.yaml INSIDE the package. The Lambda reads it at runtime
# (server/http_handler.py::_packaged_config_path) instead of via the
# OPENCONTEXT_CONFIG env var, which AWS caps at 4KB -- the config now exceeds
# that once the server `instructions` block is included.
cp config.yaml "$PACKAGE_DIR/"

# Install Python dependencies into package directory
echo "Installing Python dependencies..."

# With uv, install the exact versions in uv.lock -- the ones CI ran the
# tests against. requirements.txt only gives lower bounds (httpx>=0.28.1),
# so installing from it resolved afresh on every deploy and shipped
# library versions that nothing had tested. To take newer versions, run
# `uv lock --upgrade`, run the tests, commit uv.lock, then deploy.
# uv may be on PATH or only installed as a Python module.
UV=""
if command -v uv &> /dev/null; then
    UV="uv"
elif "$PYTHON" -m uv --version &> /dev/null; then
    UV="$PYTHON -m uv"
fi

if [ -n "$UV" ]; then
    echo "Using uv to install the locked dependencies..."
    LOCKED_REQS="$PACKAGE_DIR/.requirements-locked.txt"
    # --locked fails if uv.lock is out of date with pyproject.toml.
    if ! $UV export --locked --no-dev --no-emit-project --no-hashes \
        --output-file "$LOCKED_REQS" > /dev/null; then
        echo -e "${RED}❌ Error: uv.lock is missing or out of date with pyproject.toml${NC}"
        echo "Run 'uv lock', run the tests, commit uv.lock, then deploy."
        exit 1
    fi
    if ! $UV pip install -r "$LOCKED_REQS" \
        --no-deps \
        --target "$PACKAGE_DIR/" \
        --python-platform x86_64-manylinux2014 \
        --python-version 3.14 \
        --no-compile; then
        echo -e "${RED}❌ Error: Failed to install dependencies with uv${NC}"
        exit 1
    fi
    rm -f "$LOCKED_REQS"
else
    echo -e "${YELLOW}⚠️  uv not found: falling back to pip and requirements.txt.${NC}"
    echo -e "${YELLOW}   Versions are NOT taken from uv.lock and may differ from CI.${NC}"
    # The opencontext-* packages (opencontext-core, opencontext-arcgis-hub)
    # cannot go through this install: they come from git tags, so pip has
    # to BUILD them, and pip refuses to build anything while
    # --platform/--python-version are set. They are pure Python, so they
    # are installed in a second step with no platform flags and --no-deps;
    # the dependencies this deployment uses (pydantic, PyYAML,
    # python-json-logger, httpx) are in requirements.txt and arrive, as
    # Lambda-target wheels, with this step.
    BINARY_REQS="$PACKAGE_DIR/.requirements-binary.txt"
    GIT_REQS="$PACKAGE_DIR/.requirements-git.txt"
    grep -E '^opencontext-[a-z-]+[ @]' requirements.txt | tr -d '\r' > "$GIT_REQS" || true
    grep -vE '^opencontext-[a-z-]+[ @]' requirements.txt > "$BINARY_REQS"
    if ! pip install -r "$BINARY_REQS" -t "$PACKAGE_DIR/" \
        --platform manylinux2014_x86_64 \
        --implementation cp \
        --python-version 3.14 \
        --only-binary :all: \
        --no-compile; then
        echo -e "${RED}❌ Error: Failed to install dependencies for Lambda target platform${NC}"
        echo "Ensure pip >= 23.0 and that all packages have manylinux wheels."
        exit 1
    fi
    rm -f "$BINARY_REQS"
    if [ -s "$GIT_REQS" ]; then
        if ! pip install -r "$GIT_REQS" -t "$PACKAGE_DIR/" \
            --no-deps --no-compile; then
            echo -e "${RED}Error: Failed to install the opencontext-* packages${NC}"
            exit 1
        fi
    fi
    rm -f "$GIT_REQS"
fi

# The handler string is server.adapters.aws_lambda.lambda_handler, so a
# package without these files is a Lambda that cannot start. The plugin
# imports arcgis_hub.where_validator, so without it the one plugin cannot
# load either.
for required in core/mcp_server.py server/adapters/aws_lambda.py arcgis_hub/where_validator.py; do
    if [ ! -f "$PACKAGE_DIR/$required" ]; then
        echo -e "${RED}Error: $required is missing from the package. Are opencontext-core and opencontext-arcgis-hub in requirements.txt?${NC}"
        exit 1
    fi
done

# Record what this package was built from, so "what is live?" can be
# answered from the zip: the commit, whether the tree was dirty, and
# whether the versions came from uv.lock.
BUILD_LOCKED=false
[ -n "$UV" ] && BUILD_LOCKED=true
BUILD_DIRTY=false
[ -n "$GIT_DIRTY" ] && BUILD_DIRTY=true
printf '{"commit": "%s", "dirty": %s, "locked": %s}\n' \
    "$GIT_COMMIT" "$BUILD_DIRTY" "$BUILD_LOCKED" > "$PACKAGE_DIR/BUILD_INFO.json"
echo "Build: commit ${GIT_COMMIT:0:7} dirty=${BUILD_DIRTY} locked=${BUILD_LOCKED}"

# Create zip file
ZIP_FILE="lambda-deployment.zip"
cd "$PACKAGE_DIR"
if command -v zip &> /dev/null; then
    zip -r "../$ZIP_FILE" . > /dev/null
elif command -v powershell &> /dev/null; then
    echo "zip not found, using PowerShell Compress-Archive..."
    ABS_DEST="$(cd .. && pwd -W)/$ZIP_FILE"
    powershell -Command "Compress-Archive -Path '.\*' -DestinationPath '$ABS_DEST' -Force"
else
    echo -e "${RED}❌ Error: Neither zip nor PowerShell available to create archive${NC}"
    exit 1
fi
cd ..

echo -e "${GREEN}✓ Lambda package created: $ZIP_FILE${NC}"
echo ""

echo -e "${YELLOW}🏗️  Step 3: Deploying with Terraform...${NC}"

# Copy zip file and config.yaml to Terraform module directory
cp "$ZIP_FILE" terraform/aws/lambda-deployment.zip
cp config.yaml terraform/aws/config.yaml

# Verify the config INSIDE the built package matches the source.
# terraform/aws/config.yaml and the zip are gitignored BUILD ARTIFACTS
# that this script overwrites, so a stale one is easy to miss. It matters
# because aws.lambda_timeout surfaces as a Terraform attribute diff, but
# plugins.*.timeout ships inside the zip and only ever shows up as a
# code-hash change -- indistinguishable from a no-op plan unless you look
# in the archive.
echo -e "${YELLOW}🔍 Verifying packaged config...${NC}"
if ! $PYTHON -c "
import sys, yaml, zipfile
packaged = yaml.safe_load(zipfile.ZipFile('$ZIP_FILE').read('config.yaml'))
source = yaml.safe_load(open('config.yaml', encoding='utf-8'))
if packaged != source:
    print('Packaged config.yaml differs from the source config.yaml')
    sys.exit(1)
name = next(n for n, c in source['plugins'].items() if c.get('enabled'))
print(f\"  plugin={name} plugin_timeout={source['plugins'][name].get('timeout')}s \"
      f\"lambda_timeout={source['aws']['lambda_timeout']}s\")
"; then
    echo -e "${RED}❌ Error: packaged config does not match config.yaml${NC}"
    exit 1
fi

# Initialize Terraform if needed
if [ ! -d "terraform/aws/.terraform" ]; then
    echo "Initializing Terraform..."
    cd terraform/aws
    terraform init
    cd ../..
fi

# Plan first - validates configuration and catches errors before any changes
cd terraform/aws

echo "Selecting Terraform workspace: ${TF_WORKSPACE}"
terraform workspace select "$TF_WORKSPACE" 2>/dev/null || terraform workspace new "$TF_WORKSPACE"

echo -e "${YELLOW}📋 Planning Terraform changes...${NC}"
if ! terraform plan \
    -out=tfplan \
    -var-file="${ENVIRONMENT}.tfvars" \
    -var="aws_region=$AWS_REGION" \
    -var="config_file=config.yaml"; then
    echo -e "${RED}❌ Terraform plan failed - aborting deployment${NC}"
    exit 1
fi

# Require explicit approval before deploying
echo ""
echo -e "${YELLOW}⚠️  Deployment will apply the planned changes to AWS.${NC}"
echo -e "   Environment: ${ENVIRONMENT}"
echo -e "   Workspace:   ${TF_WORKSPACE}"
echo -e "   Lambda:      ${LAMBDA_NAME}"
echo -e "   Region:      ${AWS_REGION}"
echo ""
if [ "$AUTO_APPROVE" = "true" ]; then
    echo -e "${YELLOW}--yes supplied: applying without interactive confirmation.${NC}"
else
    read -r -p "Do you want to proceed with deployment? (yes/no): " CONFIRM
    if [ "$CONFIRM" != "yes" ] && [ "$CONFIRM" != "y" ]; then
        echo -e "${YELLOW}Deployment cancelled by user.${NC}"
        rm -f tfplan
        exit 0
    fi
fi
echo ""

# Apply the planned changes
echo -e "${YELLOW}🚀 Applying Terraform changes...${NC}"
terraform apply tfplan
rm -f tfplan

# Get URLs from Terraform output
API_GATEWAY_URL=$(terraform output -raw api_gateway_url 2>/dev/null || echo "")

cd ..

echo ""
echo -e "${GREEN}✅ Deployment complete!${NC}"
echo ""
echo "API Gateway URL (use for Claude Connectors):"
echo -e "${GREEN}$API_GATEWAY_URL${NC}"
echo ""
echo "Connect via Claude Connectors (same on Claude.ai and Claude Desktop):"
echo "  1. Go to Settings → Connectors (or Customize → Connectors on claude.ai)"
echo "  2. Click 'Add custom connector'"
echo "  3. Enter a name and URL: $API_GATEWAY_URL"
echo ""
