import boto3, os, tempfile, requests, zipfile, io, datetime

AWS_REGION = "us-west-2"
S3_BUCKET = os.getenv("SYNC_BUCKET", "o169-lambda-backups")
REPO = "oreliousw/lambda_oanda_bridge"
LAMBDA_NAMES = ["oanda_bridge", "lambda_backup_function"]

secrets = boto3.client("secretsmanager", region_name=AWS_REGION)
lambda_client = boto3.client("lambda", region_name=AWS_REGION)
s3 = boto3.client("s3", region_name=AWS_REGION)

def lambda_handler(event, context):
    # 1️⃣ Get GitHub token
    secret = secrets.get_secret_value(SecretId="github-token")
    token = secret["SecretString"]

    # 2️⃣ Download zipball of latest main branch
    url = f"https://api.github.com/repos/{REPO}/zipball/main"
    headers = {"Authorization": f"token {token}"}
    r = requests.get(url, headers=headers)
    r.raise_for_status()

    # 3️⃣ Extract and find your Lambda source zips
    tmpdir = tempfile.mkdtemp()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    z.extractall(tmpdir)

    # 4️⃣ Re-zip or pick deployable zips, upload, update Lambda
    for fn in LAMBDA_NAMES:
        zip_path = os.path.join(tmpdir, f"{fn}.zip")
        if not os.path.exists(zip_path):
            print(f"⚠️  No {fn}.zip found; skipping.")
            continue

        print(f"🚀 Updating Lambda {fn} ...")
        with open(zip_path, "rb") as f:
            lambda_client.update_function_code(
                FunctionName=fn,
                ZipFile=f.read(),
                Publish=True,
            )

    # 5️⃣ Log marker file in S3
    marker = f"sync_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"sync-logs/{marker}",
        Body=b"GitHub sync completed",
        StorageClass="STANDARD"
    )
    print("✅ GitHub → Lambda sync done.")
    return {"status": "ok"}
