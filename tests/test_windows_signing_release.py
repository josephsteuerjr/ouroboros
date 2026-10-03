"""Static guardrails for tag-only Windows Authenticode release signing.

These are not a Windows signature or a test of protected Environment settings.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source(path):
    return (ROOT / path).read_text(encoding="utf-8")


def jobs():
    workflow = source(".github/workflows/ci.yml")
    build = workflow.split("\n  build:\n", 1)[1].split("\n  windows-sign:\n", 1)[0]
    signing = workflow.split("\n  windows-sign:\n", 1)[1].split("\n  windows-proof:\n", 1)[0]
    proof = workflow.split("\n  windows-proof:\n", 1)[1].split("\n  vendor-package-smoke:\n", 1)[0]
    release = workflow.split("\n  release:\n", 1)[1]
    return build, signing, proof, release


def test_unsigned_build_and_signed_release_use_distinct_runners():
    build, signing, proof, release = jobs()
    script = source("build_windows.ps1")
    assert "environment:" not in build.split("\n    steps:\n", 1)[0]
    assert "windows-release-signing" not in build
    assert "OUROBOROS_WINDOWS_DEFER_ARCHIVE: '1'" in build
    assert script.index('OUROBOROS_WINDOWS_DEFER_ARCHIVE -eq "1"') < script.index(
        'Compress-Archive -Path "dist\\Ouroboros"'
    )
    assert "name: windows-unsigned-payload" in build
    assert "environment: windows-release-signing" in signing
    assert "needs: [build, release-preflight]" in signing
    assert signing.index("Download unsigned payload") < signing.index(
        "Sign, verify and package Windows executable"
    ) < signing.index("Transfer signed ZIP and digest")
    assert "include-hidden-files: true" in build
    assert "Smoke final Windows archive" not in signing
    assert "environment:" not in proof.split("\n    steps:\n", 1)[0]
    assert "uses: ./.github/actions/setup-python-env" in proof
    assert "needs: windows-sign" in proof
    assert "Archive executable differs from the verified signed executable" in proof
    for check in ("authenticode_signer", "timestamp", "signed_payload_archive_match"):
        assert f"--check {check}" in proof
        assert f'"{check}"' in source("scripts/release_proof.py")
    assert "name: ouroboros-windows-latest" in proof
    assert "needs.windows-proof.result == 'success'" in release
    assert "pattern: ouroboros-*" in release  # exclude intermediate unsigned payload


def test_secret_scope_and_tag_only_gate():
    build, signing_job, proof, _ = jobs()
    assert "secrets.ESIGNER_" not in proof
    assert "vars.ESIGNER_CERT_SHA1" in proof
    assert "repository-level Actions variable" in source("docs/development/14-build-and-ci.md")
    signing = source("scripts/sign_windows_release.ps1")
    assert "startsWith(github.ref, 'refs/tags/v')" in signing_job
    step = signing_job.split("- name: Sign, verify and package Windows executable", 1)[1].split(
        "- name: Locate final release archive", 1
    )[0]
    for key in ("ESIGNER_USERNAME", "ESIGNER_PASSWORD", "ESIGNER_CREDENTIAL_ID", "ESIGNER_TOTP_SECRET"):
        assert f"{key}: ${{{{ secrets.{key} }}}}" in step
        assert f"{key}: ${{{{ secrets.{key} }}}}" not in build
        assert f"{key}: ${{{{ secrets.{key} }}}}" not in signing_job.split("\n    steps:", 1)[0]
    assert "refs/tags/v$Version" in signing
    assert "GITHUB_EVENT_NAME -ne 'push'" in signing
    assert "4afc32e8b7f79bbe1de7e4e7049aaad4e0f754357613b9bbec0e3052f06fd36b" in signing
    assert "if ((Get-FileHash -LiteralPath $Download -Algorithm SHA256).Hash -ne" in signing
    assert signing.index("archive digest mismatch") < signing.index("& $Java -jar $Jar sign")
    assert "& $Tool sign" not in signing
    assert "*> $SignLog" in signing
    assert "Remove-Item -LiteralPath $Work -Recurse -Force" in signing
    assert "throw 'eSigner signing failed (private vendor log discarded)'" in signing


def test_signed_payload_and_timestamp_are_verified():
    signing = source("scripts/sign_windows_release.ps1")
    verification = source("scripts/verify_windows_signature.ps1")
    assert signing.index("-output_dir_path=$Signed") < signing.index("-Executable $SignedExe") < signing.index(
        "Copy-Item -LiteralPath $SignedExe"
    ) < signing.index("-Executable $Payload") < signing.index("ZipFile]::CreateFromDirectory")
    assert "Get-ChildItem -LiteralPath $PayloadRoot -Recurse -File -Force" in signing
    assert "Windows archive file count differs from signed payload" in signing
    assert "Get-AuthenticodeSignature -LiteralPath $Executable" in verification
    assert "$Signature.Status -ne 'Valid'" in verification
    assert "$Signature.SignerCertificate.Thumbprint -ne $ExpectedThumbprint.ToUpperInvariant()" in verification
    assert "verify /pa /tw /v $Executable" in verification
    assert "Number of warnings:\\s*0" in verification
