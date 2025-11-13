#────────────────────────────────────────────
#  start_monero.ps1 – Clean + Repaired Version
#────────────────────────────────────────────

$NodePath  = "D:\Monero\monerod.exe"
$RPC_URL   = "http://127.0.0.1:18081/json_rpc"
$StartupLog = "D:\Monero\startup_log.txt"

Write-Host "Starting Monero Daemon..." -ForegroundColor Cyan
Start-Process $NodePath -ArgumentList "--non-interactive" -WindowStyle Minimized
Start-Sleep -Seconds 3

function Test-RPC {
    try {
        $body = @{
            jsonrpc = "2.0"
            id      = "0"
            method  = "get_info"
        } | ConvertTo-Json

        $resp = Invoke-RestMethod -Uri $RPC_URL -Method Post -Body $body -ContentType "application/json"

        # Output
        $msg = "[{0}] RPC OK - Height: {1} | Synced: {2}" -f (Get-Date), $resp.height, $resp.target_height
        Add-Content -Path $StartupLog -Value $msg
        Write-Host $msg -ForegroundColor Green
        return $true
    }
    catch {
        $msg = "[{0}] RPC ERROR - {1}" -f (Get-Date), $_.Exception.Message
        Add-Content -Path $StartupLog -Value $msg
        Write-Host $msg -ForegroundColor Red
        return $false
    }
}

Write-Host "Checking RPC availability..." -ForegroundColor Yellow

$attempts = 0
while ($attempts -lt 20) {
    if (Test-RPC) { break }
    Start-Sleep -Seconds 2
    $attempts++
}

Write-Host "Done. Check $StartupLog for status entries." -ForegroundColor Cyan
