#!/bin/bash
#────────────────────────────────────────────
#  deploy_monero_healthcheck.sh
#  Builds and deploys monero_healthcheck_lambda.zip to AWS Lambda
#  Usage: ./deploy_monero_healthcheck.sh [--dry-run]
#────────────────────────────────────────────

set -e

#────────────────────────────────────────────
# ⚙️ CONFIGURATION
#────────────────────────────────────────────
LAMBDA_FUNCTION_NAME="monero_healthcheck"   # Your AWS Lambda function name
REGION="us-west-2"
ZIP_FILE="monero_healthcheck_lambda.zip"
PACKAGE_DIR="package"
SOURCE_FILE="monero_healthcheck_lambda.py"
REQUIREMENTS_FILE="requirements.txt"

#────────────────────────────────────────────
# 🏁 FLAGS
#────────────────────────────────────────────
DRY_RUN=false
if [ "$1" = "--dry-run" ]; then
    DRY_RUN=true
    echo "ℹ️ Running in dry-run mode (build only, no deployment)"
fi

#────────────────────────────────────────────
# 🔍 PREREQUISITE CHECKS
#────────────────────────────────────────────
if ! command -v pip >/dev/null 2>&1; then
    echo "❌ Error: pip is not installed"
    exit 1
fi

if ! command -v aws >/dev/null 2>&1; then
    echo "❌ Error: AWS CLI is not installed"
    exit 1
fi

if [ ! -f "$SOURCE_FILE" ]; then
    echo "❌ Error: $SOURCE_FILE not found"
    exit 1
fi

# Create minimal requirements.txt if missing
if [ ! -f "$REQUIREMENTS_FILE" ]; then
    echo "requests" > "$REQUIREMENTS_FILE"
    echo "ℹ️ Created temporary requirements.txt with 'requests'"
fi

#────────────────────────────────────────────
# 🧹 CLEAN & BUILD PACKAGE
#────────────────────────────────────────────
echo "🧹 Cleaning up existing package directory..."
rm -rf "$PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR"

echo "📦 Installing dependencies from $REQUIREMENTS_FILE..."
pip install -r "$REQUIREMENTS_FILE" -t "./$PACKAGE_DIR"

echo "📄 Copying $SOURCE_FILE to $PACKAGE_DIR..."
cp "$SOURCE_FILE" "./$PACKAGE_DIR/"

#────────────────────────────────────────────
# 🗜️ CREATE DEPLOYMENT ZIP
#────────────────────────────────────────────
echo "🗜️ Creating $ZIP_FILE..."
cd "$PACKAGE_DIR"
zip -r9 "../$ZIP_FILE" . >/dev/null
cd ..

if [ ! -f "$ZIP_FILE" ]; then
    echo "❌ Error: Failed to create $ZIP_FILE"
    exit 1
fi
echo "✅ Built $ZIP_FILE successfully"

#────────────────────────────────────────────
# 🚀 DEPLOY TO AWS LAMBDA
#────────────────────────────────────────────
if [ "$DRY_RUN" = false ]; then
    echo "🚀 Deploying $ZIP_FILE to Lambda function $LAMBDA_FUNCTION_NAME in $REGION..."
    aws lambda update-function-code \
        --function-name "$LAMBDA_FUNCTION_NAME" \
        --zip-file "fileb://$ZIP_FILE" \
        --region "$REGION"

    if [ $? -eq 0 ]; then
        echo "✅ Successfully deployed $ZIP_FILE to AWS Lambda ($LAMBDA_FUNCTION_NAME)"
    else
        echo "❌ Error: Failed to deploy to AWS Lambda"
        exit 1
    fi
else
    echo "ℹ️ Skipping deployment (dry-run mode)"
fi

#────────────────────────────────────────────
# ✅ DONE
#────────────────────────────────────────────
echo "🎉 Monero Health Check Lambda deployment completed"

