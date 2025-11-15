#────────────────────────────────────────────
# monero_healthcheck_lambda.py  (v2.0 – Node + XMRig via Cloudflare Tunnel)
# - Checks Monero node health (monerod RPC)
# - Checks XMRig mining via HTTP API exposed through Cloudflare Tunnel
# - Sends SNS alerts on failures + one daily heartbeat (UTC)
#────────────────────────────────────────────
import os
import json
import boto3
import requests
from datetime import datetime

#────────────────────────────────────────────
# 🔧 CONFIGURATION (ENV VARS RECOMMENDED)
#────────────────────────────────────────────
SNS_TOPIC_ARN   = os.getenv("SNS_TOPIC_ARN", "arn:aws:sns:us-west-2:381328847089:monero-alerts")

# Monero node (monerod) – still using your existing port/url
RPC_URL         = os.getenv("RPC_URL", "http://node.o169.com:18081")

# XMRig via Cloudflare Tunnel
XMRIG_URL       = os.getenv("XMRIG_URL", "https://xmrig.o169.com/2/summary")

# Optional: auth header (e.g., from Cloudflare Access/service token later)
XMRIG_AUTH_NAME = os.getenv("XMRIG_AUTH_HEADER_NAME", "")       # e.g. "CF-Access-Client-Id"
XMRIG_AUTH_VAL  = os.getenv("XMRIG_AUTH_HEADER_VALUE", "")      # e.g. "xxxxxxxx"

# Thresholds
MIN_HASHRATE    = float(os.getenv("MIN_HASHRATE", "500"))       # H/s minimum before warning
HEARTBEAT_HOUR  = int(os.getenv("HEARTBEAT_HOUR", "12"))        # UTC hour for daily heartbeat

sns = boto3.client("sns")


def send_sns(subject: str, message: str) -> None:
    """Publish alert message to SNS."""
    try:
        sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject, Message=message)
        print(f"SNS Sent: {subject}")
    except Exception as e:
        print(f"❌ SNS send failed: {e}")


def check_node() -> dict:
    """Check monerod health via /mining_status or /get_info-style RPC."""
    try:
        # Using your existing /mining_status endpoint
        resp = requests.post(f"{RPC_URL}/mining_status", timeout=10)
        resp.raise_for_status()
        data = resp.json()

        active  = bool(data.get("active", False))
        speed   = data.get("speed", 0)
        height  = data.get("height", "N/A")
        threads = data.get("threads_count", "N/A")

        print(f"🟢 Node OK – active={active}, speed={speed} H/s, height={height}")
        return {
            "ok": True,
            "active": active,
            "speed": speed,
            "height": height,
            "threads": threads,
            "raw": data
        }

    except Exception as e:
        msg = f"⚠️ Monero node RPC error at {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\nError: {e}"
        print(msg)
        send_sns("Monero Node RPC Error", msg)
        return {"ok": False, "error": str(e)}


def check_xmrig() -> dict:
    """Check XMRig via HTTP API exposed through Cloudflare Tunnel."""
    headers = {}
    if XMRIG_AUTH_NAME and XMRIG_AUTH_VAL:
        headers[XMRIG_AUTH_NAME] = XMRIG_AUTH_VAL

    try:
        resp = requests.get(XMRIG_URL, headers=headers, timeout=10, verify=True)
        resp.raise_for_status()
        data = resp.json()

        # Common XMRig summary fields
        hashrate_list = data.get("hashrate", {}).get("total", [0])
        hashrate = float(hashrate_list[0]) if hashrate_list else 0.0

        worker_id = data.get("worker_id", "unknown")
        algo      = data.get("algo", "unknown")
        paused    = bool(data.get("paused", False))

        print(f"🟢 XMRig OK – {hashrate} H/s, worker={worker_id}, algo={algo}, paused={paused}")

        return {
            "ok": True,
            "hashrate": hashrate,
            "worker_id": worker_id,
            "algo": algo,
            "paused": paused,
            "raw": data
        }

    except Exception as e:
        msg = f"⚠️ XMRig API error at {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\nError: {e}"
        print(msg)
        send_sns("XMRig API Error", msg)
        return {"ok": False, "error": str(e)}


#────────────────────────────────────────────
# 🩺 MAIN LAMBDA HANDLER
#────────────────────────────────────────────
def lambda_handler(event, context):
    now = datetime.utcnow()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"⏱️ Health check at {now_str}")

    # 1️⃣ Check node
    node_status = check_node()

    # 2️⃣ Check XMRig
    xmrig_status = check_xmrig()

    # 3️⃣ Evaluate health & warnings
    issues = []

    if not node_status.get("ok"):
        issues.append("Node RPC unreachable or error.")

    if not xmrig_status.get("ok"):
        issues.append("XMRig API unreachable or error.")
    else:
        # Check hashrate threshold & paused status
        h = xmrig_status["hashrate"]
        if h < MIN_HASHRATE:
            issues.append(f"XMRig hashrate low: {h} H/s (< {MIN_HASHRATE} H/s).")
        if xmrig_status["paused"]:
            issues.append("XMRig is paused.")

    # If any issues, send an aggregated warning
    if issues:
        msg = (
            f"⚠️ Monero/XMRig Health Warning at {now_str}\n\n"
            f"Node OK: {node_status.get('ok')}\n"
            f"XMRig OK: {xmrig_status.get('ok')}\n"
            f"Issues:\n - " + "\n - ".join(issues)
        )
        send_sns("Monero/XMRig Health Warning", msg)

    # 4️⃣ Once-per-day heartbeat (even if minor issues, so you know it's alive)
    if now.hour == HEARTBEAT_HOUR:
        node_height = node_status.get("height", "N/A")
        node_speed  = node_status.get("speed", "N/A")
        xm_hash     = xmrig_status.get("hashrate", "N/A")
        xm_worker   = xmrig_status.get("worker_id", "unknown")
        xm_algo     = xmrig_status.get("algo", "unknown")

        hb_msg = (
            f"✅ Daily Heartbeat – Node + Miner\n"
            f"🕒 Time (UTC): {now_str}\n\n"
            f"🧱 Node (monerod):\n"
            f"   • Height: {node_height}\n"
            f"   • Mining Speed: {node_speed} H/s (monerod miner metric)\n\n"
            f"⛏ XMRig Miner:\n"
            f"   • Hashrate: {xm_hash} H/s\n"
            f"   • Worker: {xm_worker}\n"
            f"   • Algo: {xm_algo}\n"
        )
        send_sns("Monero/XMRig Daily Heartbeat", hb_msg)

    # 5️⃣ Return combined status JSON for CloudWatch
    return {
        "status": "ok" if not issues else "warning",
        "node": node_status,
        "xmrig": xmrig_status,
        "timestamp_utc": now_str
    }
