$ErrorActionPreference = 'Stop'
$env:Path = "$HOME\.cargo\bin;C:\msys64\ucrt64\bin;$env:Path"
Push-Location $PSScriptRoot
try {
    cargo +stable-x86_64-pc-windows-gnu build --release
    Write-Host "완료: $PSScriptRoot\target\release\tradingbot-monitor.exe"
} finally {
    Pop-Location
}
