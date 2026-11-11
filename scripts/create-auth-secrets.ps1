# Run from an authenticated oc session against the intended cluster.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$namespace = 'pre-uat-finacle'
Get-Command oc -ErrorAction Stop | Out-Null

function Test-SecretExists([string] $Name) {
    $result = & oc -n $namespace get secret $Name --ignore-not-found -o name 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect Secrets. Check oc login, namespace and permissions.' }
    return [bool]$result
}

function New-AppSecret([string] $Name, [string] $Key, [string] $EncodedValue) {
    $document = @{
        apiVersion = 'v1'
        kind = 'Secret'
        metadata = @{ name = $Name; namespace = $namespace }
        type = 'Opaque'
        data = @{ $Key = $EncodedValue }
    } | ConvertTo-Json -Depth 5 -Compress
    # Send through stdin; do not place credentials in arguments or files.
    $document | & oc -n $namespace create -f - 1>$null 2>$null
    if ($LASTEXITCODE -ne 0) { throw "Cannot create $Name. Check permissions or whether another operator created it. Existing Secrets are never overwritten." }
    Write-Host "Created $Name in $namespace."
}

$ldapExists = Test-SecretExists 'finacle-support-ldap'
$authExists = Test-SecretExists 'finacle-support-auth'

if (-not $ldapExists) {
    $password = Read-Host 'LDAP bind password for openshift@mbank.local' -AsSecureString
    if ($password.Length -eq 0) { throw 'LDAP bind password cannot be empty.' }
    $pointer = [IntPtr]::Zero
    $clearPassword = $null
    $encodedPassword = $null
    try {
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($password)
        $clearPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        $encodedPassword = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($clearPassword))
        New-AppSecret 'finacle-support-ldap' 'bindPassword' $encodedPassword
    }
    finally {
        if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
        $password.Dispose()
        $clearPassword = $null
        $encodedPassword = $null
    }
} else { Write-Host 'Keeping existing finacle-support-ldap.' }

if (-not $authExists) {
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    $bytes = New-Object byte[] 48
    try {
        $random.GetBytes($bytes)
        $key = [Convert]::ToBase64String($bytes)
        New-AppSecret 'finacle-support-auth' 'SECRET_KEY' ([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($key)))
    }
    finally {
        $random.Dispose()
        [Array]::Clear($bytes, 0, $bytes.Length)
        $key = $null
    }
} else { Write-Host 'Keeping existing finacle-support-auth; active sessions are preserved.' }

Write-Host 'Secret provisioning finished. Confirm LDAP_USERNAME_ATTRIBUTE, then deploy the manifests over HTTPS.'
