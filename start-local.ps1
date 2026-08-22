$repoRoot = $PSScriptRoot
$envFile = Join-Path $env:APPDATA "coding-tools-mcp\server.env"

if (-not (Test-Path -LiteralPath $envFile)) {
    throw "找不到环境文件：$envFile"
}

foreach ($line in Get-Content -LiteralPath $envFile) {
    $line = $line.Trim()

    if (-not $line -or $line.StartsWith("#")) {
        continue
    }

    if ($line -notmatch "^[A-Za-z_][A-Za-z0-9_]*=") {
        throw "无效的环境变量行：$line"
    }

    $name, $value = $line -split "=", 2
    [Environment]::SetEnvironmentVariable(
        $name.Trim(),
        $value.Trim(),
        "Process"
    )
}

Set-Location $repoRoot
$env:PYTHONPATH = $repoRoot

python -m coding_tools_mcp.server
exit $LASTEXITCODE