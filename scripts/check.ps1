# Local CI: lint + format + types + tests + coverage gate.
# Run before committing.  Exits non-zero on any failure.

$ErrorActionPreference = "Stop"
Set-Location -Path (Join-Path $PSScriptRoot "..")

Write-Host "==> ruff check" -ForegroundColor Cyan
ruff check hybrid_agent/
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host ""
Write-Host "==> ruff format --check" -ForegroundColor Cyan
ruff format --check hybrid_agent/
if ($LASTEXITCODE -ne 0) {
    Write-Host "Run 'ruff format hybrid_agent/' to auto-fix." -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "==> mypy" -ForegroundColor Cyan
mypy hybrid_agent/
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host ""
Write-Host "==> pytest (with coverage gate)" -ForegroundColor Cyan
python -m pytest tests/ --cov=hybrid_agent --cov-report=term -q
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host ""
Write-Host "All checks passed." -ForegroundColor Green
