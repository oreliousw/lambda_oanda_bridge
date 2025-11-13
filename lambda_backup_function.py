import boto3, json, os, tempfile, tarfile, urllib.request, datetime

#─────────────────────────────────────────────
# ⚙️ Configuration
#─────────────────────────────────────────────
LAMBDA_NAMES = ["oanda_bridge", "monero_healthcheck"]
S3_BUCKET = os.getenv("BACKUP_BUCKET", "o169-lambda-backups")
AWS_REGION = os.getenv("AWS_REGION", "us-west-2")

s3 = boto3.client("s3", region_name=AWS_REGION)
lambda_client = boto3.client("lambda", region_name=AWS_REGION)

#─────────────────────────────────────────────
# 🧩 Helper: Save Lambda config + code
#─────────────────────────────────────────────
def backup_lambda(fn_name: str, dest_dir: str):
    """Downloads Lambda config and deployment package."""
    print(f"Backing up: {fn_name}")
    data = lambda_client.get_function(FunctionName=fn_name)
    cfg_path = os.path.join(dest_dir, f"{fn_name}_config.json")
    zip_path = os.path.join(dest_dir, f"{fn_name}_code.zip")

    # Save config JSON
    with open(cfg_path, "w") as f:
        json.dump(data, f, indent=2)

    # Download code ZIP
    url = data["Code"]["Location"]
    urllib.request.urlretrieve(url, zip_path)
    print(f"  ✅ Saved {fn_name}_config.json and {fn_name}_code.zip")

#─────────────────────────────────────────────
# 🧩 Main Handler
#─────────────────────────────────────────────
def lambda_handler(event, context):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
    temp_dir = tempfile.mkdtemp()
    archive_name = f"lambda_backup_{ts}.tar.gz"
    archive_path = os.path.join(temp_dir, archive_name)

    # Backup each Lambda
    for fn in LAMBDA_NAMES:
        backup_lambda(fn, temp_dir)

    # Compress to .tar.gz
    with tarfile.open(archive_path, "w:gz") as tar:
        for item in os.listdir(temp_dir):
            if item != archive_name:
                tar.add(os.path.join(temp_dir, item), arcname=item)
    print(f"🎯 Created archive: {archive_path}")

    # Upload to S3 Glacier Deep Archive
    s3.upload_file(
        archive_path,
        S3_BUCKET,
        archive_name,
        ExtraArgs={"StorageClass": "DEEP_ARCHIVE"},
    )
    print(f"☁️ Uploaded to s3://{S3_BUCKET}/{archive_name} (Deep Archive)")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "status": "ok",
            "archive": f"s3://{S3_BUCKET}/{archive_name}",
            "timestamp": ts
        })
    }
