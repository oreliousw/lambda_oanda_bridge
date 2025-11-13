#!/bin/bash
set -e

LAMBDA_FUNCTION_NAME="lambda_repo_sync"
REGION="us-west-2"
ZIP_FILE="lambda_repo_sync.zip"
PACKAGE_DIR="package"
SOURCE_FILE="lambda_repo_sync.py"
REQUIREMENTS_FILE="requirements.txt"

rm -rf "$PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR"

pip install -r "$REQUIREMENTS_FILE" -t "./$PACKAGE_DIR"
cp "$SOURCE_FILE" "./$PACKAGE_DIR/"
cd "$PACKAGE_DIR" && zip -r9 "../$ZIP_FILE" . && cd ..
echo "✅ Built $ZIP_FILE"

aws lambda update-function-code \
  --function-name "$LAMBDA_FUNCTION_NAME" \
  --zip-file "fileb://$ZIP_FILE" \
  --region "$REGION" || echo "⚠️ Function not found — run create-function first."
