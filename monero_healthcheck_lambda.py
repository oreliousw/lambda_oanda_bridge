#────────────────────────────────────────────
# monero_healthcheck_lambda.py  (v1.3 – AWS Lambda)
# Sends SNS alerts only on failures, plus one daily "Node Healthy" heartbeat
#────────────────────────────────────────────
import os, boto3, requests
from datetime import datetime

#────────────────────────────────────────────
# 🔧 CONFIGURATION
#────────────────────────────────────────────
SNS_TOPIC_ARN = os.getenv("SNS_TOPIC_ARN", "arn:aws:sns:us-west-2:381328847089:monero-alerts")
RPC_URL       = os.getenv("RPC_URL", "http://node.o169.com:18081")
MINER_ADDRESS = os.getenv("MINER_ADDRESS", "48GugGo1NLXDV59yV2n7kfdTZJSWqPHBvCBsS6Z48ZnqWLGnD4nbiT9CeRJNQtgeyBew7JfSiTp5fRqhe9E6cPBuLPHwTte")
THREADS_COUNT = int(os.getenv("THREADS_COUNT", "9"))
HEARTBEAT_HOUR = int(os.getenv("HEARTBEAT_HOUR", "12"))  # UTC hour to send daily OK ping (default noon)

sns = boto3.client("sns")

def send_sns(subject, message):
    """Publish alert message to SNS."""
    try:
        sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject, Message=message)
        print(f"SNS Sent: {subject}")
    except Exception as e:
        print(f"❌ SNS send failed: {e}")

#────────────────────────────────────────────
# 🩺 MAIN HEALTH CHECK
#────────────────────────────────────────────
def lambda_handler(event, context):
    now = datetime.utcnow()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"⏱️ Health check at {now_str}")

    # 1️⃣ Query mining status
    try:
        resp = requests.post(f"{RPC_URL}/mining_status", timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        msg = f"⚠️ RPC error contacting node at {now_str}\nError: {e}"
        send_sns("Monero RPC Error", msg)
        return {"status": "rpc_error", "error": str(e)}

    # 2️⃣ Node responded
    if data.get("active", False):
        # Normal operation
        print(f"✅ Node healthy at height {data.get('height','N/A')} – {data.get('speed','N/A')} H/s")

        # 🕛 Once-per-day heartbeat (at specified UTC hour)
        if now.hour == HEARTBEAT_HOUR:
            msg = (
                f"✅ Daily Heartbeat – Node healthy and mining\n"
                f"🕒 Time: {now_str}\n"
                f"💨 Hashrate: {data.get('speed', 'N/A')} H/s\n"
                f"🧵 Threads: {data.get('threads_count', THREADS_COUNT)}"
            )
            send_sns("Monero Node Heartbeat", msg)
        return {"status": "ok"}

    # 3️⃣ Mining inactive — attempt restart
    warn_msg = f"⚠️ Mining inactive at {now_str}. Attempting restart..."
    print(warn_msg)
    send_sns("Monero Mining Warning", warn_msg)

    try:
        body = {
            "miner_address": MINER_ADDRESS,
            "threads_count": THREADS_COUNT,
            "do_background_mining": False
        }
        restart = requests.post(f"{RPC_URL}/start_mining", json=body, timeout=10)
        restart.raise_for_status()

        msg = f"✅ Mining restart command sent successfully at {now_str}."
        send_sns("Monero Auto-Restart", msg)
        return {"status": "restart_sent"}

    except Exception as e:
        err = f"❌ Failed to restart mining at {now_str}\nError: {e}"
        send_sns("Monero Restart Error", err)
        return {"status": "restart_failed", "error": str(e)}
