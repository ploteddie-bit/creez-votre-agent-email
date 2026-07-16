# ============================================================
# Agent Mail 24/7 — bootstrap (PowerShell natif Windows)
#
# Équivalent Windows de bootstrap.sh pour les environnements où
# bash / Git Bash n'est pas disponible.
#
# Prépare un environnement fonctionnel :
#   1. Vérifie Python 3.11+
#   2. Crée configs/.env et configs/config.yaml depuis les exemples
#   3. Crée le venv et installe les dépendances
#   4. Alerte sur les placeholders à remplir
#
# Idempotent. Usage :
#   .\bootstrap.ps1
# ============================================================

#Requires -Version 5.0
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Write-Step([string]$msg) { Write-Host "[bootstrap] $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg)   { Write-Host "[bootstrap] $msg" -ForegroundColor Green }
function Write-Warn([string]$msg) { Write-Host "[bootstrap] $msg" -ForegroundColor Yellow }
function Write-Err([string]$msg)  { Write-Host "[bootstrap] $msg" -ForegroundColor Red }

Write-Host "=== Agent Mail 24/7 — Bootstrap (PowerShell) ===" -ForegroundColor Yellow
Write-Host ""

# --- 1. Python 3.11+ ---
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pyCmd) {
    $pyCmd = Get-Command python3 -ErrorAction SilentlyContinue
}
if (-not $pyCmd) {
    Write-Err "Python introuvable. Installe Python 3.11+ (https://www.python.org/) puis relance ce script."
    exit 1
}
$python = $pyCmd.Source

$versionOutput = & $python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
if ($LASTEXITCODE -ne 0 -or -not $versionOutput) {
    Write-Err "Impossible de déterminer la version de Python."
    exit 1
}
$parts = $versionOutput.Split('.')
$major = [int]$parts[0]; $minor = [int]$parts[1]
Write-Step "Python $versionOutput detecte ($python)."
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 11)) {
    Write-Err "Python $versionValue detecte — Python 3.11+ requis."
    exit 1
}

# --- 2. Configuration ---
if (-not (Test-Path "configs")) { New-Item -ItemType Directory -Path "configs" | Out-Null }

if (-not (Test-Path "configs/config.yaml")) {
    Copy-Item "configs/config.yaml.example" "configs/config.yaml"
    Write-Ok "configs/config.yaml cree depuis l'exemple."
} else {
    Write-Step "configs/config.yaml existe deja — inchange."
}

if (-not (Test-Path "configs/.env")) {
    Copy-Item "configs/.env.example" "configs/.env"
    Write-Ok "configs/.env cree depuis l'exemple."
} else {
    Write-Step "configs/.env existe deja — inchange."
}

# --- 3. Vérification des placeholders ---
Write-Warn "Verification des placeholders a completer..."
$placeholders = $false
if ((Get-Content "configs/config.yaml" -Raw) -match "10\.0\.0\.XXX") {
    Write-Warn "  - configs/config.yaml contient encore '10.0.0.XXX' (IP PostgreSQL / Ollama / dashboard)."
    $placeholders = $true
}
if ((Get-Content "configs/.env" -Raw) -match "(?m)^EMAIL_LEARNER_DB_PASSWORD=$") {
    Write-Warn "  - configs/.env : EMAIL_LEARNER_DB_PASSWORD est vide."
    $placeholders = $true
}
if ((Get-Content "configs/.env" -Raw) -match "(?m)^EMAIL_LEARNER_GMAIL_CLIENT_ID=$") {
    Write-Warn "  - configs/.env : EMAIL_LEARNER_GMAIL_CLIENT_ID est vide (OAuth Gmail)."
    $placeholders = $true
}
if ($placeholders) {
    Write-Warn "Edite configs/config.yaml et configs/.env avant de lancer le daemon."
} else {
    Write-Ok "Aucun placeholder evident detecte dans la configuration."
}

# --- 4. venv + dépendances ---
if (-not (Test-Path "venv")) {
    Write-Step "Creation du virtualenv .\venv ..."
    & $python -m venv venv
}

$venvPython = if (Test-Path "venv\Scripts\python.exe") { "venv\Scripts\python.exe" } else { "venv\bin\python" }
if (-not (Test-Path $venvPython)) {
    Write-Err "Le venv a ete cree mais python.exe est introuvable dedans."
    exit 1
}

Write-Step "Installation des dependances..."
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r requirements.txt --quiet
Write-Ok "Dependances installees."

# --- 5. Smoke test ---
Write-Step "Smoke test : import de src.config..."
& $venvPython -c "import src.config" 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Ok "src.config importe avec succes — la configuration est lisible."
} else {
    Write-Warn "src.config n'a pas pu etre importe (configs/config.yaml ou configs/.env peut etre mal forme)."
}

Write-Host ""
Write-Host "=== Bootstrap termine ===" -ForegroundColor Green
Write-Host ""
Write-Host "Prochaines etapes :"
Write-Host "  1. Edite configs/config.yaml (IP PostgreSQL, Ollama, bind dashboard)"
Write-Host "  2. Edite configs/.env (mot de passe DB, credentials OAuth Gmail)"
Write-Host "  3. Place tes credentials Gmail dans configs/gmail-credentials.json"
Write-Host "     (vois docs/oauth-setup.md pour le pas-a-pas GCP)"
Write-Host "  4. Initialise la base :"
Write-Host "       venv\Scripts\python.exe -m alembic upgrade head"
Write-Host "  5. Lance le daemon :"
Write-Host "       venv\Scripts\python.exe -m src.main"
Write-Host "     ou le dashboard :"
Write-Host "       venv\Scripts\python.exe -m src.main dashboard"
Write-Host ""
