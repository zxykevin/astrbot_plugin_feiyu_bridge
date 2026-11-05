param(
    [switch]$CancelOnly,
    [switch]$StatusOnly,
    [string]$SshTarget = $env:FEIYU_E2E_SSH_TARGET
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($SshTarget)) {
    throw 'set FEIYU_E2E_SSH_TARGET (for example user@host)'
}

$bridgeSecret = (& ssh $SshTarget `
    'sudo -n docker exec feiyu-bridge-cross-e2e printenv FEIYU_E2E_BRIDGE_SECRET').Trim()
$controlSecret = (& ssh $SshTarget `
    'sudo -n docker exec feiyu-bridge-cross-e2e printenv FEIYU_E2E_CONTROL_SECRET').Trim()
$dashboardPassword = (& ssh $SshTarget `
    'sudo -n docker exec feiyu-bridge-cross-e2e printenv FEIYU_E2E_DASHBOARD_PASSWORD').Trim()
if (-not $bridgeSecret -or -not $controlSecret -or -not $dashboardPassword) {
    throw 'isolated credentials are unavailable'
}

$loginBody = @{
    username = 'astrbot'
    password = $dashboardPassword
} | ConvertTo-Json -Compress
$login = Invoke-RestMethod -Method Post `
    -Uri 'http://127.0.0.1:16187/api/auth/login' `
    -ContentType 'application/json' -Body $loginBody
$dashboardToken = [string]$login.data.token
if (-not $dashboardToken) {
    throw 'isolated dashboard login failed'
}

$keyBody = @{
    name = 'feiyu-cross-e2e-control'
    scopes = @('plugin')
    expires_in_days = 1
} | ConvertTo-Json -Compress
$keyResponse = Invoke-RestMethod -Method Post `
    -Uri 'http://127.0.0.1:16187/api/apikey/create' `
    -Headers @{Authorization = "Bearer $dashboardToken"} `
    -ContentType 'application/json' -Body $keyBody
$apiKey = [string]$keyResponse.data.api_key
if (-not $apiKey) {
    throw 'isolated API key creation failed'
}

$env:FEIYU_E2E_BRIDGE_SECRET = $bridgeSecret
$env:FEIYU_E2E_CONTROL_SECRET = $controlSecret
$env:FEIYU_E2E_API_KEY = $apiKey
try {
    $runnerArgs = @((Join-Path $PSScriptRoot 'real_cross_e2e.py'))
    if ($CancelOnly) {
        $runnerArgs += '--cancel-only'
    }
    if ($StatusOnly) {
        $runnerArgs += '--status-only'
    }
    & python @runnerArgs
    if ($LASTEXITCODE -ne 0) {
        throw 'cross-system E2E runner failed'
    }
}
finally {
    Remove-Item Env:FEIYU_E2E_BRIDGE_SECRET -ErrorAction SilentlyContinue
    Remove-Item Env:FEIYU_E2E_CONTROL_SECRET -ErrorAction SilentlyContinue
    Remove-Item Env:FEIYU_E2E_API_KEY -ErrorAction SilentlyContinue
    $bridgeSecret = $null
    $controlSecret = $null
    $dashboardPassword = $null
    $dashboardToken = $null
    $apiKey = $null
}
