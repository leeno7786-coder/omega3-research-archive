# Deterministic memory routing — full regression (Phases 1–7)
# Usage: .\scripts\routing_regression.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$env:PYTEST_ADDOPTS = ""
$Py = Join-Path $Root ".venv-brain\Scripts\python.exe"

if (-not (Test-Path $Py)) {
    Write-Error "Missing venv: $Py"
}

Write-Host "=== Routing harness metrics ===" -ForegroundColor Cyan
& $Py scripts\routing_harness_report.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "`n=== Pytest regression suites ===" -ForegroundColor Cyan
& $Py -m pytest -o addopts="" -q `
    tests\memory\test_deterministic_routing_stress.py `
    tests\memory\test_phase7_routing_harness.py `
    tests\memory\test_phase6_classifier_retirement.py `
    tests\memory\test_phase5_undecided_hold.py `
    tests\memory\test_gate_single_lobe_lock.py `
    tests\memory\test_ingest_subdomain_override.py `
    tests\auto_loop\test_subdomain_grammar.py `
    tests\auto_loop\test_subdomain_recall.py `
    tests\core\test_persona_router_fallback.py `
    tests\memory `
    tests\core\test_failure_log_batch2.py

exit $LASTEXITCODE
