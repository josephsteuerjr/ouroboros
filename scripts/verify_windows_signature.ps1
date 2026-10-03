# Verify the Windows release payload before and after packaging. Never receives credentials.
param(
    [Parameter(Mandatory = $true)][string]$Executable,
    [Parameter(Mandatory = $true)][string]$ExpectedThumbprint
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { throw 'Windows executable missing' }
if ($ExpectedThumbprint -notmatch '^[a-fA-F0-9]{40}$') { throw 'Expected SHA-1 certificate thumbprint is missing or malformed' }
$Signature = Get-AuthenticodeSignature -LiteralPath $Executable
if ($Signature.Status -ne 'Valid' -or -not $Signature.SignerCertificate) {
    throw "Authenticode signature is not valid: $($Signature.Status)"
}
if ($Signature.SignerCertificate.Thumbprint -ne $ExpectedThumbprint.ToUpperInvariant()) {
    throw 'Authenticode signer does not match the configured certificate'
}
# RFC 3161 timestamps need not populate TimeStamperCertificate in this API.
# signtool /tw below is the timestamp authority; any warning fails closed.
$SignTool = Get-Command signtool.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source
if (-not $SignTool) {
    $Kits = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
    if (Test-Path $Kits) {
        $SignTool = Get-ChildItem $Kits -Directory | Sort-Object Name -Descending |
            ForEach-Object { Join-Path $_.FullName 'x64\signtool.exe' } |
            Where-Object { Test-Path $_ } | Select-Object -First 1
    }
}
if (-not $SignTool) { throw 'Windows SDK signtool.exe is required for timestamp verification' }
$VerifyLog = Join-Path $env:RUNNER_TEMP ("windows-sign-verify-$PID.log")
try {
    & $SignTool verify /pa /tw /v $Executable *> $VerifyLog
    if ($LASTEXITCODE -ne 0) { throw 'Windows SDK signature verification failed' }
    $Output = Get-Content -Raw -LiteralPath $VerifyLog
    if ($Output -notmatch 'Number of warnings:\s*0' -or $Output -notmatch 'Successfully verified') {
        throw 'Windows SDK did not confirm a warning-free timestamped signature'
    }
} finally {
    Remove-Item -LiteralPath $VerifyLog -Force -ErrorAction SilentlyContinue
}
Write-Host "Verified signed executable SHA-256: $((Get-FileHash -LiteralPath $Executable -Algorithm SHA256).Hash)"
