#!/bin/bash
#─────────────────────────────────────────────
#  AWS Lambda Backup Script (Mr O)
#  Creates timestamped backups for both:
#   - oanda_bridge
#   - monero_healthcheck
#  Saves code + config JSONs into ~/lambda_backups
#─────────────────────────────────────────────

# Exit on error
set -e

# 📅 Timestamp label
DATE=$(date +"%Y-%m-%d_%H-%M-%S")

# 📁 Backup directory
BACKUP_DIR=~/lambda_backups/$DATE
mkdir -p "$BACKUP_DIR"

# 🧩 Functions to back up
FUNCS=("oanda_bridge" "monero_healthcheck")

echo "🔹 Starting Lambda backup on $DATE"
for FN in "${FUNCS[@]}"; do
  echo "Backing up function: $FN"

  # 🧠 Get function configuration + metadata
  aws lambda get-function --function-name "$FN" \
    > "$BACKUP_DIR/${FN}_config.json"

  # 💾 Download the deployment package (.zip)
  CODE_URL=$(jq -r '.Code.Location' "$BACKUP_DIR/${FN}_config.json")
  curl -s -o "$BACKUP_DIR/${FN}_code.zip" "$CODE_URL"

  echo "✅ Saved: ${FN}_config.json and ${FN}_code.zip"
done

# 🗜️ Optional compression
tar -czf ~/lambda_backups/lambda_backup_${DATE}.tar.gz -C ~/lambda_backups "$DATE"
echo "🎉 All backups completed → ~/lambda_backups/lambda_backup_${DATE}.tar.gz"

# Optional: upload tarball to S3
S3_BUCKET="s3://o169-lambda-backups"
aws s3 cp ~/lambda_backups/lambda_backup_${DATE}.tar.gz $S3_BUCKET/ || echo "⚠️ S3 upload skipped (bucket missing or perms)"
