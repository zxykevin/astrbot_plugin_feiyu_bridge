param(
    [string]$SshTarget = $env:FEIYU_E2E_SSH_TARGET,
    [string]$DshExecutable = $env:FEIYU_DSH_EXECUTABLE,
    [string]$DshBin = $env:FEIYU_DSH_BIN
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($SshTarget)) {
    throw 'set FEIYU_E2E_SSH_TARGET (for example user@host)'
}
if ([string]::IsNullOrWhiteSpace($DshExecutable) -or [string]::IsNullOrWhiteSpace($DshBin)) {
    throw 'set FEIYU_DSH_EXECUTABLE and FEIYU_DSH_BIN'
}

$bridgeSecret = [Convert]::ToHexString(
    [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
).ToLower()
$controlSecret = [Convert]::ToHexString(
    [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
).ToLower()
$dashboardPassword = 'E2E' + [Convert]::ToHexString(
    [Security.Cryptography.RandomNumberGenerator]::GetBytes(12)
)

$remoteCommand = @(
    'sudo -n docker run -d'
    '--name feiyu-bridge-cross-e2e'
    '--memory 280m --memory-swap 420m --cpus 0.40 --pids-limit 256'
    '-p 127.0.0.1:16187:6185'
    "-e FEIYU_E2E_BRIDGE_SECRET=$bridgeSecret"
    "-e FEIYU_E2E_CONTROL_SECRET=$controlSecret"
    "-e FEIYU_E2E_DASHBOARD_PASSWORD=$dashboardPassword"
    '-v /tmp/feiyu_bridge_cross_e2e_server.tar.gz:/tmp/server.tar.gz:ro'
    '-v /tmp/feiyu_bridge_cross_e2e_driver.tar.gz:/tmp/driver.tar.gz:ro'
    '-v /tmp/feiyu_bridge_cross_e2e_init.py:/tmp/init.py:ro'
    "soulter/astrbot:latest sh -c 'python /tmp/init.py && exec python main.py'"
) -join ' '
$containerId = & ssh $SshTarget $remoteCommand
if ($LASTEXITCODE -ne 0 -or -not $containerId) {
    throw 'failed to start isolated AstrBot'
}

$tunnel = Start-Process -FilePath 'ssh.exe' -ArgumentList @(
    '-N', '-o', 'ExitOnForwardFailure=yes',
    '-L', '16187:127.0.0.1:16187',
    $SshTarget
) -WindowStyle Hidden -PassThru

$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 1
    try {
        $null = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 `
            'http://127.0.0.1:16187/api/auth/setup-status'
        $ready = $true
        break
    }
    catch {}
}
if (-not $ready) {
    throw 'isolated AstrBot did not become ready'
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
    throw 'dashboard login returned no token'
}

$keyBody = @{
    name = 'feiyu-cross-e2e'
    scopes = @('plugin')
    expires_in_days = 1
} | ConvertTo-Json -Compress
$keyResponse = Invoke-RestMethod -Method Post `
    -Uri 'http://127.0.0.1:16187/api/apikey/create' `
    -Headers @{Authorization = "Bearer $dashboardToken"} `
    -ContentType 'application/json' -Body $keyBody
$apiKey = [string]$keyResponse.data.api_key
if (-not $apiKey) {
    throw 'API key creation returned no key'
}

$env:FEIYU_BRIDGE_SECRET = $bridgeSecret
$env:FEIYU_ASTRBOT_API_KEY = $apiKey
$env:ELECTRON_RUN_AS_NODE = '1'
$stdout = Join-Path $env:TEMP 'feiyu-cross-e2e-dsh.stdout.log'
$stderr = Join-Path $env:TEMP 'feiyu-cross-e2e-dsh.stderr.log'
$dsh = Start-Process -FilePath $DshExecutable `
    -ArgumentList @(
        $dshBin, '--profile', 'feiyu-dev', '--port', '6198', '--no-open'
    ) `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -PassThru
Remove-Item Env:FEIYU_BRIDGE_SECRET
Remove-Item Env:FEIYU_ASTRBOT_API_KEY
Remove-Item Env:ELECTRON_RUN_AS_NODE

$secretFile = Join-Path $env:TEMP 'feiyu-cross-e2e.secrets.json'
$record = @{
    bridge = ConvertFrom-SecureString (
        ConvertTo-SecureString $bridgeSecret -AsPlainText -Force
    )
    control = ConvertFrom-SecureString (
        ConvertTo-SecureString $controlSecret -AsPlainText -Force
    )
    api = ConvertFrom-SecureString (
        ConvertTo-SecureString $apiKey -AsPlainText -Force
    )
    tunnel_pid = $tunnel.Id
    dsh_pid = $dsh.Id
    container = 'feiyu-bridge-cross-e2e'
    base_url = 'http://127.0.0.1:16187'
} | ConvertTo-Json -Compress
[IO.File]::WriteAllText(
    $secretFile, $record, [Text.UTF8Encoding]::new($false)
)
$null = & icacls $secretFile /inheritance:r /grant:r "$env:USERNAME`:(R,W)"

$bridgeSecret = $null
$controlSecret = $null
$dashboardPassword = $null
$dashboardToken = $null
$apiKey = $null

Write-Output 'ISOLATED_ASTRBOT_STARTED'
Write-Output 'SSH_FORWARD_READY http://127.0.0.1:16187'
Write-Output 'DSH_FEIYU_DEV_STARTED'
