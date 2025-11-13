import boto3, json, os, tempfile, tarfile, urllib.request, datetime, traceback

#─────────────────────────────────────────────
# ⚙️ Configuration
#─────────────────────────────────────────────
LAMBDA_NAMES = ["oanda_bridge", "monero_healthcheck"]
S3_BUCKET = os.getenv("BACKUP_BUCKET", "o169-lambda-backups")
AWS_REGION = os.getenv("AWS_REGION", "us-west-2")
SNS_TOPIC_ARN = os.getenv("SNS_TOPIC_ARN", "arn:aws:sns:us-west-2:381328847089:lambda-backup-alerts")

s3 = boto3.client("s3", region_name=AWS_REGION)
lambda_client = boto3.client("lambda", region_name=AWS_REGION)
sns = boto3.client("sns", region_name=AWS_REGION)

#─────────────────────────────────────────────
# 🧩 Helper: SNS Notify
#─────────────────────────────────────────────
def send_sns(subject, message):
    try:
        sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject, Message=message)
        print(f"📣 SNS sent: {subject}")
    except Exception as e:
        print(f"⚠️ SNS send failed: {e}")

#─────────────────────────────────────────────
# 🧩 Helper: Save Lambda config + code
#─────────────────────────────────────────────
def backup_lambda(fn_name: str, dest_dir: str):
    print(f"Backing up: {fn_name}")
    data = lambda_client.get_function(FunctionName=fn_name)
    cfg_path = os.path.join(dest_dir, f"{fn_name}_config.json")
    zip_path = os.path.join(dest_dir, f"{fn_name}_code.zip")
    with open(cfg_path, "w") as f:
        json.dump(data, f, indent=2)
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

    try:
        for fn in LAMBDA_NAMES:
            backup_lambda(fn, temp_dir)

        with tarfile.open(archive_path, "w:gz") as tar:
            for item in os.listdir(temp_dir):
                if item != archive_name:
                    tar.add(os.path.join(temp_dir, item), arcname=item)
        print(f"🎯 Created archive: {archive_path}")

        s3.upload_file(
            archive_path,
            S3_BUCKET,
            archive_name,
            ExtraArgs={"StorageClass": "DEEP_ARCHIVE"},
        )
        msg = f"✅ Lambda backup completed.\nArchive: s3://{S3_BUCKET}/{archive_name}\nTime: {ts}"
        send_sns("Lambda Backup Success", msg)

        return {"statusCode": 200, "body": json.dumps({"status": "ok", "archive": msg})}

    except Exception as e:
        err_msg = f"❌ Backup failed at {ts}\nError: {e}\nTrace:\n{traceback.format_exc()}"
        send_sns("Lambda Backup Failure", err_msg)
        print(err_msg)
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}

