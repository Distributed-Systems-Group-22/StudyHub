$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    $envPath = Join-Path $projectRoot '.env'
    if (-not (Test-Path -LiteralPath $envPath)) {
        Copy-Item -LiteralPath '.env.example' -Destination $envPath
    }

    $envText = [IO.File]::ReadAllText($envPath)
    $originalEnvText = $envText
    foreach ($key in 'STUDYHUB_PROCESSING_TOKEN', 'STUDYHUB_SEARCH_TOKEN', 'STUDYHUB_SYNTHESIS_TOKEN') {
        $assignment = "(?m)^[ \t]*${key}[ \t]*="
        $emptyAssignment = $assignment + '[ \t]*(?=\r?$)'
        if ($envText -match $emptyAssignment) {
            $token = [guid]::NewGuid().ToString('N')
            $envText = [regex]::Replace($envText, $emptyAssignment, "$key=$token")
        } elseif ($envText -notmatch $assignment) {
            $token = [guid]::NewGuid().ToString('N')
            $envText = $envText.TrimEnd() + "`r`n$key=$token`r`n"
        }
    }

    foreach ($line in Get-Content -LiteralPath '.env.example') {
        if ($line -match '^([A-Z][A-Z0-9_]*)=') {
            $key = $Matches[1]
            if ($envText -notmatch "(?m)^[ \t]*${key}[ \t]*=") {
                $envText = $envText.TrimEnd() + "`r`n$line`r`n"
            }
        }
    }
    if ($envText -ne $originalEnvText) {
        [IO.File]::WriteAllText($envPath, $envText, [Text.UTF8Encoding]::new($false))
    }

    Write-Host 'Configuration ready. Run docker compose up --build -d.'
} finally {
    Pop-Location
}
