#────────────────────────────────────────────
#  start_monero_service.ps1 – FULL XYZ Edition
#────────────────────────────────────────────

$NodePath   = "D:\Monero\monero-gui-v0.18.4.3\monerod.exe"
$Args       = "--non-interactive"
$DataDir    = "D:\Monero\monero-gui-v0.18.4.3"
$StartupLog = "D:\Monero\startup_log.txt"
$RPC_URL    = "http://127.0.0.1:18081/json_rpc"

# SNS Topic
$SNS_Topic  = "arn:aws:sns:us-west-2:381328847089:monero-alerts"

# Alert rate limiting
$LastAlertTime = (Get-Date).AddMinutes(-20)

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date), $msg
    Add-Content -Path $StartupLog -Value $line
    Write-Host $line
}

function Send-SNSAlert($subject, $message) {
    $global:LastAlertTime

    # Prevent alert spam (15 minutes)
    if ((Get-Date) -lt $LastAlertTime.AddMinutes(15)) {
        return
    }

    $LastAlertTime = Get-Date

    try {
        aws sns publish `
            --topic-arn $SNS_Topic `
            --subject "$subject" `
            --message "$message" | Out-Null

        Log "SNS Alert Sent: $subject"
    }
    catch {
        Log "SNS ERROR: $($_.Exception.Message)"
    }
}
function Test-RPC {
    try {
        $body = @{
            jsonrpc = "2.0"
            id      = "0"
            method  = "get_info"
        } | ConvertTo-Json

        return Invoke-RestMethod -Uri $RPC_URL -Method Post -Body $body -ContentType "application/json"
    }
    catch {
        return $null
    }
}

function Check-Disk {
    $drive = Get-PSDrive C
    $freeGB = [math]::Round($drive.Free / 1GB, 2)

    if ($freeGB -lt 10) {
        Send-SNSAlert "Monero Low Disk Space" "Only $freeGB GB free on C:. Node may stop syncing."
        Log "Low disk space: $freeGB GB"
    }
}

function Rotate-Logs {
    if (Test-Path $StartupLog) {
        $maxSizeMB = 50
        if ((Get-Item $StartupLog).Length / 1MB -gt $maxSizeMB) {
            $backup = "D:\Monero\startup_log_{0}.txt" -f (Get-Date -Format "yyyyMMdd_HHmm")
            Move-Item $StartupLog $backup -Force
            Log "Log rotated to $backup"
        }
    }
}
Log "Starting Monero node (FULL XYZ edition)..."

while ($true) {

    Log "Launching monerod..."
    $process = Start-Process -FilePath $NodePath -ArgumentList $Args `
        -WorkingDirectory $DataDir -NoNewWindow -PassThru

    $lastHeight = 0
    $stalledCount = 0
    $syncedSent = $false

    while (-not $process.HasExited) {

        Start-Sleep -Seconds 10

        Rotate-Logs
        Check-Disk

        $resp = Test-RPC

        if ($null -eq $resp) {
            Log "RPC DOWN"
            Send-SNSAlert "Monero RPC DOWN" "The Monero RPC is not responding."
            continue
        }

        $height = $resp.height
        $target = $resp.target_height
        $peers  = $resp.incoming_connections_count + $resp.outgoing_connections_count
        $majorVersion = $resp.version

        Log "RPC OK: Height=$height Target=$target Peers=$peers Version=$majorVersion"

        # Peer count monitoring
        if ($peers -lt 4) {
            Send-SNSAlert "Low Peer Count" "Node has $peers peers."
        }

        # Sync stall detection
        if ($height -eq $lastHeight) {
            $stalledCount++
            if ($stalledCount -ge 3) {
                Send-SNSAlert "Sync Stalled" "Blockchain height stuck at $height."
            }
        }
        else {
            $stalledCount = 0
        }

        # Fully synced alert
        if (-not $syncedSent -and $target -gt 0 -and ($height -ge $target - 2)) {
            Send-SNSAlert "Monero Fully Synced" "Height $height (target $target)."
            $syncedSent = $true
        }

        # Version mismatch alert
        if ($majorVersion -lt 18) {
            Send-SNSAlert "Outdated Monero Node" "Node version $majorVersion detected."
        }

        $lastHeight = $height
    }

    # Process exited
    $exitCode = $process.ExitCode
    Log "monerod exited with code $exitCode"
    Send-SNSAlert "Monero Node Crash" "monerod exited with code $exitCode. Restarting..."

    Log "Restarting monerod in 10 seconds..."
    Start-Sleep -Seconds 10
}

