[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)]
    [int] $Port = 5000,
    [switch] $PromptBindPassword
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv-local\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $python = Join-Path $projectRoot '.venv\Scripts\python.exe'
}
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Local Python environment not found. Create .venv and install requirements.txt first.'
}
& $python -c 'import flask, ldap3' 2>$null
if ($LASTEXITCODE -ne 0) { throw "Install dependencies first: & '$python' -m pip install -r requirements.txt" }

$settings = @{
    LDAP_URL = 'ldap://172.18.31.10:389'
    LDAP_BASE_DN = 'dc=mbank,dc=local'
    LDAP_USERNAME_ATTRIBUTE = 'sAMAccountName'
    LDAP_BIND_DN = 'openshift@mbank.local'
    LDAP_INSECURE = 'true'
    SESSION_COOKIE_SECURE = 'false'
    NAMESPACE = 'pre-uat-finacle'
    PORT = [string]$Port
}
$previous = @{}
foreach ($name in @($settings.Keys) + @('LDAP_BIND_PASSWORD', 'SECRET_KEY')) {
    $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
$password = $null
$pointer = [IntPtr]::Zero
$random = $null
$bytes = New-Object byte[] 48
try {
    foreach ($name in $settings.Keys) {
        [Environment]::SetEnvironmentVariable($name, $settings[$name], 'Process')
    }
    if ($PromptBindPassword -or -not $env:LDAP_BIND_PASSWORD) {
        $password = Read-Host 'LDAP bind password for openshift@mbank.local' -AsSecureString
        if ($password.Length -eq 0) { throw 'LDAP bind password cannot be empty.' }
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($password)
        $env:LDAP_BIND_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    $random.GetBytes($bytes)
    $env:SECRET_KEY = [Convert]::ToBase64String($bytes)
    Write-Host "Open http://127.0.0.1:$Port/login and sign in with your sAMAccountName and password."
    Write-Host 'LDAP bind password stays in this process environment. Ctrl+C stops the server.'
    Write-Host 'Dashboard cluster operations use your current oc login; authenticate oc before using them.'
    Push-Location -LiteralPath $projectRoot
    try {
        & $python -c "import os; from app import app; app.run(host='127.0.0.1', port=int(os.environ['PORT']), debug=False, use_reloader=False)"
        if ($LASTEXITCODE -ne 0) { throw 'Local web server exited with an error.' }
    }
    finally { Pop-Location }
}
finally {
    foreach ($name in $previous.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process')
    }
    if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    if ($null -ne $password) { $password.Dispose() }
    if ($null -ne $random) { $random.Dispose() }
    [Array]::Clear($bytes, 0, $bytes.Length)
}
