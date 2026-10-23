#Requires -Version 7.3
<#
.SYNOPSIS
  Creates the KITE production environment in Azure: App Service (Linux, Python) and its own
  Azure Database for PostgreSQL flexible server. Run once; it is safe to re-run (existing resources are kept).

.EXAMPLE
  az login
  .\deploy\azure\provision.ps1 -ResourceGroup rg-kite-prod -AppName kite-prod -PgServer kite-prod-pg

  Then set the Microsoft sign-in and mail settings (see README, "Production on Azure App Service")
  and deploy the code with .\deploy\azure\deploy.ps1.

.NOTES
  Creates billable resources. Generated secrets (SECRET_KEY, database password) go straight into the
  App Service settings and are not printed. Developed by: Arun Arumugam.
#>
param(
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [Parameter(Mandatory)] [string] $AppName,          # becomes https://<AppName>.azurewebsites.net
    [Parameter(Mandatory)] [string] $PgServer,         # becomes <PgServer>.postgres.database.azure.com
    [string] $Location = "centralindia",
    [string] $AppSku = "B1",                           # App Service plan; P0v3 or higher for production load
    [string] $PgSku = "Standard_B1ms",                 # burstable; General Purpose for heavier use
    [string] $PgTier = "Burstable",
    [string] $PgVersion = "16",
    [string] $PgAdminUser = "kiteadmin",
    [string] $Database = "kite",
    [string] $BaseUrl = ""                             # custom https host name; default the azurewebsites.net one
)
$ErrorActionPreference = "Stop"

function New-Secret([int] $bytes = 32) {
    $buf = New-Object byte[] $bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($buf)
    -join ($buf | ForEach-Object { $_.ToString("x2") })
}

if (-not $BaseUrl) { $BaseUrl = "https://$AppName.azurewebsites.net" }
$plan = "$AppName-plan"

Write-Host "Resource group $ResourceGroup ($Location)"
az group create --name $ResourceGroup --location $Location --output none

# --- PostgreSQL (production only; never shared with the Vercel/Neon test database) ---------------
$pgExists = az postgres flexible-server list --resource-group $ResourceGroup --query "[?name=='$PgServer'].name" -o tsv
$pgPassword = $null
if (-not $pgExists) {
    $pgPassword = "Fx" + (New-Secret 18) + "!9"
    Write-Host "PostgreSQL flexible server $PgServer (takes a few minutes)"
    az postgres flexible-server create `
        --resource-group $ResourceGroup --name $PgServer --location $Location `
        --tier $PgTier --sku-name $PgSku --version $PgVersion --storage-size 32 `
        --admin-user $PgAdminUser --admin-password $pgPassword `
        --public-access 0.0.0.0 --backup-retention 14 --yes --output none
} else {
    Write-Host "PostgreSQL server $PgServer already exists; keeping it and its password."
}
az postgres flexible-server db create --resource-group $ResourceGroup --server-name $PgServer `
    --database-name $Database --output none
# TLS is required by default on flexible server; make sure it stays on.
az postgres flexible-server parameter set --resource-group $ResourceGroup --server-name $PgServer `
    --name require_secure_transport --value on --output none

# --- App Service ---------------------------------------------------------------------------------
Write-Host "App Service plan $plan ($AppSku, Linux) and web app $AppName"
az appservice plan create --resource-group $ResourceGroup --name $plan --location $Location `
    --is-linux --sku $AppSku --output none
$appExists = az webapp list --resource-group $ResourceGroup --query "[?name=='$AppName'].name" -o tsv
if (-not $appExists) {
    az webapp create --resource-group $ResourceGroup --plan $plan --name $AppName `
        --runtime "PYTHON:3.12" --output none
}

az webapp config set --resource-group $ResourceGroup --name $AppName `
    --startup-file "gunicorn --bind=0.0.0.0:8000 --workers 2 --timeout 120 --access-logfile - wsgi:app" `
    --min-tls-version 1.2 --ftps-state Disabled --http20-enabled true --always-on true `
    --generic-configurations '{"healthCheckPath": "/healthz"}' --output none
az webapp update --resource-group $ResourceGroup --name $AppName --https-only true --output none
az webapp log config --resource-group $ResourceGroup --name $AppName `
    --application-logging filesystem --docker-container-logging filesystem --level information --output none

# Settings. Secrets are only written when first created, so re-running never rotates them by accident.
$existing = az webapp config appsettings list --resource-group $ResourceGroup --name $AppName --query "[].name" -o tsv
$settings = @(
    "APP_ENV=production",
    "APP_BASE_URL=$BaseUrl",
    "SCM_DO_BUILD_DURING_DEPLOYMENT=true",
    "PGHOST=$PgServer.postgres.database.azure.com",
    "PGUSER=$PgAdminUser",
    "PGDATABASE=$Database",
    "PGSSLMODE=require",
    "SESSION_HOURS=8",
    "SSO_ENABLED=false",
    "LOCAL_LOGIN_ENABLED=true"
)
if ($existing -notcontains "SECRET_KEY") { $settings += "SECRET_KEY=$(New-Secret 32)" }
if ($pgPassword) { $settings += "PGPASSWORD=$pgPassword" }
az webapp config appsettings set --resource-group $ResourceGroup --name $AppName --settings $settings --output none

Write-Host ""
Write-Host "Done. Production site: $BaseUrl"
Write-Host "Next:"
Write-Host "  1. Deploy the code:        .\deploy\azure\deploy.ps1 -ResourceGroup $ResourceGroup -AppName $AppName"
Write-Host "  2. Create the first admin: App Service -> SSH, then: python seed.py   (see README)"
Write-Host "  3. Microsoft sign-in:      add AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, then SSO_ENABLED=true"
Write-Host "  4. Mail:                   SMTP_* settings or Admin console -> Mail settings"
