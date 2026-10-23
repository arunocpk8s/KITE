#Requires -Version 7.3
<#
.SYNOPSIS
  Deploys the committed KITE code to Azure App Service as a zip (App Service installs requirements.txt).

.DESCRIPTION
  The package is built from the last commit (git archive), so .env, docs/, the SQLite database and
  anything uncommitted are never uploaded. The company logo (static/logo.*), which is kept out of the
  public repository, is added from this PC when present, so the production site shows it.

.EXAMPLE
  .\deploy\azure\deploy.ps1 -ResourceGroup rg-kite-prod -AppName kite-prod

.NOTES
  Developed by: Arun Arumugam.
#>
param(
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [Parameter(Mandatory)] [string] $AppName,
    [switch] $NoLogo
)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$zip = Join-Path ([System.IO.Path]::GetTempPath()) "kite-deploy-$([guid]::NewGuid().ToString('N')).zip"

try {
    Push-Location $root
    $dirty = git status --porcelain --untracked-files=no
    if ($dirty) { Write-Warning "Uncommitted changes are NOT deployed (only the last commit is):`n$dirty" }
    # Tests, deploy scripts and CI files are not needed on the server.
    git archive --format=zip --output $zip HEAD -- . ":(exclude)tests" ":(exclude)deploy" ":(exclude).github"
    if ($LASTEXITCODE) { throw "git archive failed" }
    Pop-Location

    if (-not $NoLogo) {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [System.IO.Compression.ZipFile]::Open($zip, "Update")
        try {
            Get-ChildItem "$root\static" -Filter "logo.*" -ErrorAction SilentlyContinue | ForEach-Object {
                [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $_.FullName, "static/$($_.Name)") | Out-Null
                Write-Host "Including static/$($_.Name)"
            }
        } finally { $archive.Dispose() }
    }

    Write-Host "Uploading $([math]::Round((Get-Item $zip).Length / 1KB)) KB to $AppName (build runs on App Service)..."
    az webapp deploy --resource-group $ResourceGroup --name $AppName --src-path $zip --type zip --async false
    if ($LASTEXITCODE) { throw "az webapp deploy failed" }
    $hostName = az webapp show --resource-group $ResourceGroup --name $AppName --query defaultHostName -o tsv
    Write-Host "Deployed. Health check: https://$hostName/healthz"
}
finally {
    Remove-Item -Force $zip -ErrorAction SilentlyContinue
}
