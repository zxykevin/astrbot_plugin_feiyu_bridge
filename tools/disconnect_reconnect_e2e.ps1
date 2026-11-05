param(
    [string]$SshTarget = $env:FEIYU_E2E_SSH_TARGET
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($SshTarget)) {
    throw 'set FEIYU_E2E_SSH_TARGET (for example user@host)'
}

$bridgeSecret = (& ssh $SshTarget `
    'sudo -n docker exec feiyu-bridge-cross-e2e printenv FEIYU_E2E_BRIDGE_SECRET').Trim()
$dashboardPassword = (& ssh $SshTarget `
    'sudo -n docker exec feiyu-bridge-cross-e2e printenv FEIYU_E2E_DASHBOARD_PASSWORD').Trim()

$loginBody = @{
    username = 'astrbot'
    password = $dashboardPassword
} | ConvertTo-Json -Compress
$login = Invoke-RestMethod -Method Post `
    -Uri 'http://127.0.0.1:16187/api/auth/login' `
    -ContentType 'application/json' -Body $loginBody
$dashboardToken = [string]$login.data.token
$keyBody = @{
    name = 'feiyu-reconnect-e2e'
    scopes = @('plugin')
    expires_in_days = 1
} | ConvertTo-Json -Compress
$keyResponse = Invoke-RestMethod -Method Post `
    -Uri 'http://127.0.0.1:16187/api/apikey/create' `
    -Headers @{Authorization = "Bearer $dashboardToken"} `
    -ContentType 'application/json' -Body $keyBody
$apiKey = [string]$keyResponse.data.api_key

$observer = Start-Process -FilePath 'ssh.exe' -ArgumentList @(
    '-N', '-o', 'ExitOnForwardFailure=yes',
    '-L', '16188:127.0.0.1:16187',
    $SshTarget
) -WindowStyle Hidden -PassThru
Start-Sleep -Seconds 2

$mainTunnel = Get-NetTCPConnection -LocalPort 16187 -State Listen |
    Select-Object -First 1 -ExpandProperty OwningProcess
if (-not $mainTunnel) {
    throw 'main E2E tunnel is not listening'
}
Stop-Process -Id $mainTunnel
Start-Sleep -Seconds 30

$env:FEIYU_E2E_BRIDGE_SECRET = $bridgeSecret
$env:FEIYU_E2E_API_KEY = $apiKey
$env:FEIYU_E2E_CONTROL_SECRET = 'unused-status-only-value'
$env:FEIYU_E2E_BASE_URL = 'http://127.0.0.1:16188'
try {
    Write-Output 'STATUS_WHILE_DSH_DISCONNECTED'
    & python (Join-Path $PSScriptRoot 'real_cross_e2e.py') --status-only
    $replacement = Start-Process -FilePath 'ssh.exe' -ArgumentList @(
        '-N', '-o', 'ExitOnForwardFailure=yes',
        '-L', '16187:127.0.0.1:16187',
        $SshTarget
    ) -WindowStyle Hidden -PassThru
    Write-Output 'STATUS_AFTER_TUNNEL_RESTORED'
    & python (Join-Path $PSScriptRoot 'real_cross_e2e.py') `
        --status-only --wait-online 45
}
finally {
    Stop-Process -Id $observer.Id -ErrorAction SilentlyContinue
    Remove-Item Env:FEIYU_E2E_BRIDGE_SECRET -ErrorAction SilentlyContinue
    Remove-Item Env:FEIYU_E2E_CONTROL_SECRET -ErrorAction SilentlyContinue
    Remove-Item Env:FEIYU_E2E_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:FEIYU_E2E_BASE_URL -ErrorAction SilentlyContinue
    $bridgeSecret = $null
    $dashboardPassword = $null
    $dashboardToken = $null
    $apiKey = $null
}
