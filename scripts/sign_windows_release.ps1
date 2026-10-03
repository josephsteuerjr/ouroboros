# Tag-only release step. SSL.com CodeSignTool 1.3.2 is pinned by exact archive digest.
# The eSigner account password and TOTP seed are supplied through the protected
# GitHub Environment to this step only. They never enter checked-in files.
$ErrorActionPreference = 'Stop'
$Required = @('ESIGNER_USERNAME', 'ESIGNER_PASSWORD', 'ESIGNER_CREDENTIAL_ID', 'ESIGNER_TOTP_SECRET', 'ESIGNER_CERT_SHA1')
foreach ($Name in $Required) {
    if (-not [Environment]::GetEnvironmentVariable($Name)) { throw "Missing Windows signing configuration: $Name" }
}
if ($env:GITHUB_EVENT_NAME -ne 'push') { throw 'Windows release signing requires a pushed release tag' }
$Version = (Get-Content VERSION).Trim()
if ($env:GITHUB_REF -ne "refs/tags/v$Version") { throw 'Windows release tag must match VERSION exactly' }
if (-not $env:RUNNER_TEMP) { throw 'Windows release signing requires a temporary runner directory' }
$Payload = (Resolve-Path 'dist\Ouroboros\Ouroboros.exe').Path
$Archive = "dist\Ouroboros-$Version-windows-x64.zip"
if (Test-Path $Archive) { throw 'Unsigned Windows archive unexpectedly exists' }
$Work = Join-Path $env:RUNNER_TEMP ("esigner-" + [guid]::NewGuid().ToString("N"))
$Download = Join-Path $Work 'CodeSignTool-v1.3.2-windows.zip'
$Extract = Join-Path $Work 'tool'
$Signed = Join-Path $Work 'signed'
$SignLog = Join-Path $Work 'sign.log'
try {
    New-Item -ItemType Directory -Force -Path $Extract, $Signed | Out-Null
    # SSL.com's own GitHub release asset: a versioned URL whose bytes never move,
    # unlike the download page's unversioned "current version" link.
    Invoke-WebRequest -Uri 'https://github.com/SSLcom/CodeSignTool/releases/download/v1.3.2/CodeSignTool-v1.3.2-windows.zip' -OutFile $Download
    $ExpectedDigest = '4afc32e8b7f79bbe1de7e4e7049aaad4e0f754357613b9bbec0e3052f06fd36b'
    if ((Get-FileHash -LiteralPath $Download -Algorithm SHA256).Hash -ne $ExpectedDigest.ToUpperInvariant()) {
        throw 'CodeSignTool archive digest mismatch'
    }
    Expand-Archive -LiteralPath $Download -DestinationPath $Extract
    # The vendor BAT expands %* through cmd.exe without quoting. Invoke its
    # bundled Java directly so password metacharacters cannot become commands.
    # The archive is digest-pinned, so its layout is fixed (jar\, conf\,
    # jdk-11.0.2\ and AppleDouble __MACOSX\ junk at the top level); locating
    # the JAR and the bundled JDK spares this script the JDK folder name.
    $JarItem = Get-ChildItem -LiteralPath $Extract -Recurse -File -Filter 'code_sign_tool-*.jar' |
        Where-Object { $_.FullName -notmatch '__MACOSX' } | Select-Object -First 1
    $JavaItem = Get-ChildItem -LiteralPath $Extract -Recurse -File -Filter 'java.exe' |
        Where-Object { $_.Directory.Name -eq 'bin' -and $_.FullName -notmatch '__MACOSX' } | Select-Object -First 1
    if (-not $JarItem -or -not $JavaItem) { throw 'Pinned CodeSignTool runtime missing' }
    $Jar = $JarItem.FullName
    $Java = $JavaItem.FullName
    $ToolRoot = $JarItem.Directory.Parent.FullName  # jar\ sits beside conf\ in the tool root
    # Vendor CLI still takes credentials on argv. Only the fresh trusted
    # signing runner may receive them; never echo or retain vendor output.
    try {
        Push-Location $ToolRoot  # vendor JAR resolves conf/ relative to its tool root
        try {
            & $Java -jar $Jar sign "-username=$env:ESIGNER_USERNAME" "-password=$env:ESIGNER_PASSWORD" `
                "-credential_id=$env:ESIGNER_CREDENTIAL_ID" "-totp_secret=$env:ESIGNER_TOTP_SECRET" `
                "-input_file_path=$Payload" "-output_dir_path=$Signed" *> $SignLog
            if ($LASTEXITCODE -ne 0) { throw 'signer returned nonzero' }
        } finally { Pop-Location }
    } catch {
        # Never render a vendor exception: it may include the command line.
        throw 'eSigner signing failed (private vendor log discarded)'
    }
    $SignedExe = Join-Path $Signed 'Ouroboros.exe'
    & "$PSScriptRoot\verify_windows_signature.ps1" -Executable $SignedExe -ExpectedThumbprint $env:ESIGNER_CERT_SHA1
    Copy-Item -LiteralPath $SignedExe -Destination $Payload -Force
    & "$PSScriptRoot\verify_windows_signature.ps1" -Executable $Payload -ExpectedThumbprint $env:ESIGNER_CERT_SHA1
    # Compress-Archive silently skips OS-Hidden entries. ZipFile walks the
    # entire payload, including Playwright's .local-browsers and hidden files.
    $PayloadRoot = (Resolve-Path 'dist\Ouroboros').Path
    $ArchivePath = Join-Path (Resolve-Path 'dist').Path (Split-Path $Archive -Leaf)
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $PayloadRoot, $ArchivePath, [System.IO.Compression.CompressionLevel]::Optimal, $true)
    $AuditRoot = Join-Path $Work 'archive-audit'
    Expand-Archive -LiteralPath $ArchivePath -DestinationPath $AuditRoot
    $ExtractedRoot = Join-Path $AuditRoot 'Ouroboros'
    $SourceFiles = @(Get-ChildItem -LiteralPath $PayloadRoot -Recurse -File -Force)
    $ExtractedFiles = @(Get-ChildItem -LiteralPath $ExtractedRoot -Recurse -File -Force)
    if ($SourceFiles.Count -ne $ExtractedFiles.Count) { throw 'Windows archive file count differs from signed payload' }
    foreach ($File in $SourceFiles) {
        $Relative = $File.FullName.Substring($PayloadRoot.Length).TrimStart('\')
        $InArchive = Join-Path $ExtractedRoot $Relative
        if (-not (Test-Path -LiteralPath $InArchive -PathType Leaf) -or
            (Get-FileHash -LiteralPath $File.FullName -Algorithm SHA256).Hash -ne
            (Get-FileHash -LiteralPath $InArchive -Algorithm SHA256).Hash) {
            throw "Windows archive payload differs at $Relative"
        }
    }
    Write-Host "Windows release ZIP SHA-256: $((Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash)"
} finally {
    Remove-Item -LiteralPath $Work -Recurse -Force -ErrorAction SilentlyContinue
}
