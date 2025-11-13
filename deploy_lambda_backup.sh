#!/bin/bash
#─────────────────────────────────────────────
#  Lambda Backup Deployment Script (Mr O)
#  Builds and deploys lambda_backup_function.zip to AWS Lambda
#  Supports optional --dry-run for build-only mode
#─────────────────────────────────────────────

set -e

# Configuration
LAMBDA_FUNCTION_NAME="lambda_backup_function"
REGION="us-west-2"
ZIP_FILE="lambda_backup_function.zip"
PACKAGE_DIR="package"
SOURCE_FILE="lambda_backup_function.py"
REQUIREMENTS_FILE="requirements.txt"

# Check for dry-run flag
DRY_RUN=false
if [ "$1" = "--dry-run" ]; then
    DRY_RUN=true
    echo "ℹ️ Running in dry-run mode (build only, no deployment)"
fi

# Check prerequisites
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

# Allow requirements.txt to be optional (this Lambda uses boto3 already)
if [ ! -f "$REQUIREMENTS_FILE" ]; then
    echo "⚠️  Warning: $REQUIREMENTS_FILE not found — skipping dependency install"
    touch "$REQUIREMENTS_FILE"
fi

# Clean up existing package directory
echo "🧹 Cleaning up existing package directory..."
rm -rf "$PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR"

# Install dependencies (if any)
echo "📦 Installing dependencies from $REQUIREMENTS_FILE..."
pip install -r "$REQUIREMENTS_FILE" -t "./$PACKAGE_DIR"

# Copy source file
echo "📄 Copying $SOURCE_FILE to $PACKAGE_DIR..."
cp "$SOURCE_FILE" "./$PACKAGE_DIR/"

# Create ZIP file
echo "🗜️ Creating $ZIP_FILE..."
cd "$PACKAGE_DIR"
zip -r9 "../$ZIP_FILE" .
cd ..

# Verify ZIP file creation
if [ ! -f "$ZIP_FILE" ]; then
    echo "❌ Error: Failed to create $ZIP_FILE"
    exit 1
fi
echo "✅ Built $ZIP_FILE"

# Deploy to AWS Lambda (unless dry-run)
if [ "$DRY_RUN" = false ]; then
    echo "🚀 Deploying $ZIP_FILE to Lambda function $LAMBDA_FUNCTION_NAME in $REGION..."
    aws lambda update-function-code \
        --function-name "$LAMBDA_FUNCTION_NAME" \
        --zip-file "fileb://$ZIP_FILE" \
        --region "$REGION"
    if [ $? -eq 0 ]; then
        echo "✅ Successfully deployed $ZIP_FILE to AWS Lambda"
    else
        echo "❌ Error: Failed to deploy to AWS Lambda"
        exit 1
    fi
else
    echo "ℹ️ Skipping deployment (dry-run mode)"
fi

# Optional cleanup
# echo "🧹 Cleaning up $ZIP_FILE..."
# rm -f "$ZIP_FILE"

echo "🎉 Lambda backup deployment completed successfully"
