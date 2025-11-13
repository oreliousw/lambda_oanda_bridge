import boto3, os, tempfile, requests, zipfile, io, datetime, traceback

#─────────────────────────────────────────────
# ⚙️ Configuration
#─────────────────────────────────────────────
AWS_REGION = "us-west-2"
REPO = "oreliousw/lambda_oanda_bridge"
LAMBDA_NAMES = ["oanda_bridge", "lambda_backup_function"]
S3_BUCKET = os.getenv("SYNC_BUCKET", "o169-lambda-backups")
SNS_TOPIC_ARN = os.getenv(
    "SNS_TOPIC_ARN",
    "arn:aws:sns:us-west-2:381328847089:lambda-sync-alerts"
)
SECRET_ID = os.getenv("GITHUB_SECRET_ID", "github-token")

#─────────────────────────────────────────────
# 🔧 AWS Clients
#─────────────────────────────────────────────
s3 = boto3.client("s3", region_name=AWS_REGION)
sns = boto3.client("sns", region_name=AWS_REGION)
lambda_client = boto3.client("lambda", region_name=AWS_REGION)
secrets = boto3.client("secretsmanager", region_name=AWS_REGION)

#─────────────────────────────────────────────
# 📨 Helper – Send SNS message
#─────────────────────────────────────────────
def send_sns(subject, message):
    try:
        sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject, Message=message)
        print(f"📣 SNS sent: {subject}")
    except Exception as e:
        print(f"⚠️ SNS send failed: {e}")

#─────────────────────────────────────────────
# 🧩 Main Lambda Handler
#─────────────────────────────────────────────
def lambda_handler(event, context):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
    try:
        # 1️⃣ Get GitHub token
        secret = secrets.get_secret_value(SecretId=SECRET_ID)
        token = secret["SecretString"]

        # 2️⃣ Download latest zipball from GitHub
        url = f"https://api.github.com/repos/{REPO}/zipball/main"
        headers = {"Authorization": f"token {token}"}
        r = requests.get(url, headers=headers)
        r.raise_for_status()

        tmpdir = tempfile.mkdtemp()
        z = zipfile.ZipFile(io.BytesIO(r.content))
        z.extractall(tmpdir)

        # 3️⃣ Look for deployable zips and update Lambdas
        updated = []
        for fn in LAMBDA_NAMES:
            zip_path = os.path.join(tmpdir, f"{fn}.zip")
            if os.path.exists(zip_path):
                print(f"🚀 Updating Lambda {fn} ...")
                with open(zip_path, "rb") as f:
                    lambda_client.update_function_code(
                        FunctionName=fn,
                        ZipFile=f.read(),
                        Publish=True,
                    )
                updated.append(fn)
            else:
                print(f"⚠️ No {fn}.zip found, skipped.")

        # 4️⃣ Log marker file in S3
        marker = f"sync_{ts}.txt"
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=f"sync-logs/{marker}",
            Body=("\n".join(updated) or "No functions updated").encode("utf-8"),
            StorageClass="STANDARD"
        )

        msg = f"✅ GitHub sync completed at {ts}\nUpdated Lambdas: {updated or 'none'}"
        send_sns("Lambda Repo Sync Success", msg)
        print(msg)

        return {"statusCode": 200, "body": msg}

    except Exception as e:
        err = f"❌ Sync failed at {ts}\nError: {e}\nTrace:\n{traceback.format_exc()}"
        send_sns("Lambda Repo Sync Failure", err)
        print(err)
        return {"statusCode": 500, "body": err}
