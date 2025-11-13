#────────────────────────────────────────────
#  start_monero_service.ps1 – Service Version
#────────────────────────────────────────────

$NodePath  = "D:\Monero\monerod.exe"
$Args      = "--non-interactive"
$StartupLog = "D:\Monero\startup_log.txt"
$RPC_URL   = "http://127.0.0.1:18081/json_rpc"

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date), $msg
    Add-Content -Path $StartupLog -Value $line
    Write-Host $line
}

Log "Starting Monero node (service mode)..."

while ($true) {

    # Launch monerod in the FOREGROUND so service stays alive
    Log "Launching monerod..."
    $process = Start-Process -FilePath $NodePath -ArgumentList $Args -NoNewWindow -PassThru

    # Monitor loop
    while (-not $process.HasExited) {
        Start-Sleep -Seconds 10

        # Optional — light RPC monitor
        try {
            $body = @{
                jsonrpc = "2.0"
                id      = "0"
                method  = "get_info"
            } | ConvertTo-Json

            $resp = Invoke-RestMethod -Uri $RPC_URL -Method Post -Body $body -ContentType "application/json"

            Log "RPC OK: Height=$($resp.height), Target=$($resp.target_height)"
        }
        catch {
            Log "RPC ERROR: $($_.Exception.Message)"
        }
    }

    # If monerod exits, log it & restart it
    Log "monerod exited with code $($process.ExitCode). Restarting in 10 seconds..."
    Start-Sleep -Seconds 10
}
