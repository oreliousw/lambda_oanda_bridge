# ⭐ **OANDA Bridge + Lambda Automation Suite**
### *Serverless automated trading, backup, and Monero monitoring — built for AWS + TradingView*

<p align="left">
  <img src="https://img.shields.io/badge/AWS_Lambda-Automated-orange?style=for-the-badge&logo=awslambda&logoColor=white"/>
  <img src="https://img.shields.io/badge/OANDA-API-blue?style=for-the-badge&logo=oanda&logoColor=white"/>
  <img src="https://img.shields.io/badge/TradingView-Auto_Trading-green?style=for-the-badge&logo=tradingview&logoColor=white"/>
  <img src="https://img.shields.io/badge/Monero-Healthcheck-orange?style=for-the-badge&logo=monero&logoColor=white"/>
  <img src="https://img.shields.io/badge/GitHub-Repo_Sync-yellow?style=for-the-badge&logo=github&logoColor=white"/>
</p>

---

# ⚡ **Quick Start**
### *Deploy the bridge, connect TradingView, and start automated trading in minutes.*

```bash
git clone https://github.com/oreliousw/lambda_oanda_bridge.git
cd lambda_oanda_bridge
pip install -r requirements.txt
./deploy_oanda_bridge.sh
```

### **TradingView Webhook URL**
```
https://your-api-gateway-url/webhook
```

### **TradingView JSON**
```json
{
  "message": "BUY",
  "instrument": "USD_CAD",
  "entry": 1.4010,
  "sl": 1.4045,
  "tp": 1.3950,
  "risk_pct": 20
}
```

---

# 🧩 **Architecture**

```
               ┌──────────────────────────────────┐
               │         TradingView Alerts        │
               └──────────────────────────────────┘
                                │
                                ▼
                  ┌────────────────────────┐
                  │ API Gateway /webhook   │
                  └────────────────────────┘
                                │
                                ▼
                  ┌────────────────────────┐
                  │   oanda_bridge.py      │
                  └────────────────────────┘
                                │
                                ▼
                   ┌─────────────────────┐
                   │   OANDA REST API    │
                   └─────────────────────┘

                                │
     ┌────────────────────────────────────────────────────────────┐
     │                          AWS Lambda                        │
     │------------------------------------------------------------│
     │ lambda_backup_function.py   → S3 Deep Archive backups      │
     │ lambda_repo_sync.py         → GitHub → Lambda auto-sync    │
     │ monero_healthcheck_lambda.py→ Heartbeats + auto-restart    │
     └────────────────────────────────────────────────────────────┘

                                │
                                ▼
                      ┌─────────────────┐
                      │      SNS        │
                      └─────────────────┘
```

---

# 📦 **Modules Overview**

| Module | Description |
|--------|-------------|
| **oanda_bridge.py** | TradingView → OANDA auto-execution. |
| **monero_healthcheck_lambda.py** | Monero RPC monitoring + restart. |
| **lambda_backup_function.py** | Backup Lambda code/config to S3. |
| **lambda_repo_sync.py** | Auto-redeploy Lambda from GitHub. |

---

# 📘 **Module Docs**

## 🟦 **1. OANDA Bridge – `oanda_bridge.py`**
Handles:
- BUY / SELL / CLOSE  
- Risk-based lot sizing  
- Live pip calculations  
- 3‑pip SL/TP enforcement  
- DRY_RUN mode  
- `/ping` health endpoint  

## 🟧 **2. Monero Healthcheck – `monero_healthcheck_lambda.py`**
- Monitors mining status  
- Auto-restarts mining  
- Daily heartbeat  
- SNS alerts  

## 🟪 **3. Lambda Backup – `lambda_backup_function.py`**
- Saves Lambda code + config  
- Creates `.tar.gz` archive  
- Uploads to S3 Deep Archive  

## 🟩 **4. Repo Sync – `lambda_repo_sync.py`**
- Pulls repo ZIP from GitHub  
- Updates Lambda functions  
- Logs changes to S3  
- SNS notifications  

---

# 🔌 **TradingView Alert Setup**

**Webhook URL**
```
https://YOUR_API_ID.execute-api.us-west-2.amazonaws.com/webhook
```

**JSON Example**
```json
{
  "message": "SELL",
  "instrument": "EURUSD",
  "entry": 1.0832,
  "sl": 1.0860,
  "tp": 1.0780,
  "risk_pct": 25
}
```

---

# 🛡 **Security**
- Uses AWS Secrets Manager  
- IAM‑restricted execution roles  
- SNS alerting for failures  
- Serverless = no persistent public attack surface  

---

# 🎉 **Done**
Just copy this file into your GitHub `README.md` and push.

