param(
    [Parameter(Mandatory = $true)]
    [string]$JsonPath
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$EnvPath = Join-Path $ProjectRoot ".env"

if (-not (Test-Path -LiteralPath $JsonPath)) {
    throw "Self-client JSON file not found: $JsonPath"
}

$Config = Get-Content -LiteralPath $JsonPath -Raw | ConvertFrom-Json
$RequiredFields = @("client_id", "client_secret", "code", "grant_type")

foreach ($Field in $RequiredFields) {
    if ([string]::IsNullOrWhiteSpace([string]$Config.$Field)) {
        throw "The self-client JSON is missing the required '$Field' value."
    }
}

if ($Config.expiry_time) {
    $Expiry = [DateTimeOffset]::FromUnixTimeMilliseconds([int64]$Config.expiry_time)
    if ($Expiry -le [DateTimeOffset]::UtcNow) {
        throw "The grant code expired at $($Expiry.UtcDateTime.ToString('u')). Generate a fresh self-client JSON and retry immediately."
    }
}

$TokenResponse = Invoke-RestMethod `
    -Method Post `
    -Uri "https://accounts.zoho.in/oauth/v2/token" `
    -ContentType "application/x-www-form-urlencoded" `
    -Body @{
        client_id     = $Config.client_id
        client_secret = $Config.client_secret
        grant_type    = "authorization_code"
        code          = $Config.code
    }

if ([string]::IsNullOrWhiteSpace([string]$TokenResponse.refresh_token)) {
    throw "Zoho did not return a refresh token. Generate the grant for offline access and retry with a new code."
}

if (Test-Path -LiteralPath $EnvPath) {
    $Lines = [System.Collections.Generic.List[string]](
        Get-Content -LiteralPath $EnvPath
    )
}
else {
    $Lines = [System.Collections.Generic.List[string]]::new()
}

function Set-EnvValue {
    param(
        [string]$Name,
        [string]$Value
    )

    $Prefix = "$Name="
    for ($Index = 0; $Index -lt $Lines.Count; $Index++) {
        if ($Lines[$Index].StartsWith($Prefix, [StringComparison]::Ordinal)) {
            $Lines[$Index] = "$Prefix$Value"
            return
        }
    }

    $Lines.Add("$Prefix$Value")
}

Set-EnvValue "ZOHO_ACCOUNTS_URL" "https://accounts.zoho.in"
Set-EnvValue "ZOHO_API_DOMAIN" ([string]$TokenResponse.api_domain)
Set-EnvValue "ZOHO_CLIENT_ID" ([string]$Config.client_id)
Set-EnvValue "ZOHO_CLIENT_SECRET" ([string]$Config.client_secret)
Set-EnvValue "ZOHO_REFRESH_TOKEN" ([string]$TokenResponse.refresh_token)

Set-Content -LiteralPath $EnvPath -Value $Lines -Encoding utf8

Write-Host "Zoho OAuth succeeded. The project's .env file has been updated."
Write-Host "API domain: $($TokenResponse.api_domain)"
Write-Host "Access-token lifetime: $($TokenResponse.expires_in) seconds"

$Config = $null
$TokenResponse = $null
