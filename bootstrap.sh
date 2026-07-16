#!/usr/bin/env bash
# Agent Mail 24/7 - Script de bootstrap
# Cree configs/.env et configs/config.yaml depuis les examples
# Idempotent : ne fait rien si les fichiers existent deja

set -e

cd "$(dirname "$0")"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${YELLOW}=== Agent Mail 24/7 - Bootstrap ===${NC}"
echo ""

# 1. Verifier que Python 3.11+ est dispo
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}ERREUR: python3 introuvable. Installer Python 3.11+ avant de continuer.${NC}"
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Python detecte: $PYTHON_VERSION"

# 2. Creer le venv si manquant
if [ ! -d "venv" ]; then
    echo -e "${YELLOW}Creation du venv...${NC}"
    python3 -m venv venv
    echo -e "${GREEN}venv cree${NC}"
else
    echo -e "${YELLOW}venv existe deja, skip${NC}"
fi

# Activer le venv pour la suite
# shellcheck disable=SC1091
source venv/bin/activate

# 3. Installer les dependances
echo -e "${YELLOW}Installation des dependances...${NC}"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
echo -e "${GREEN}Dependances installees${NC}"

# 4. Creer configs/.env depuis l'example
if [ ! -f "configs/.env" ]; then
    cp configs/.env.example configs/.env
    echo -e "${GREEN}configs/.env cree${NC}"
    echo -e "${RED}>>> EDITER configs/.env et remplir EMAIL_LEARNER_DB_PASSWORD et GMAIL_CLIENT_SECRET${NC}"
else
    echo -e "${YELLOW}configs/.env existe deja, skip${NC}"
fi

# 5. Creer configs/config.yaml depuis l'example
if [ ! -f "configs/config.yaml" ]; then
    cp configs/config.yaml.example configs/config.yaml
    echo -e "${GREEN}configs/config.yaml cree${NC}"
    echo -e "${RED}>>> EDITER configs/config.yaml et remplacer 10.0.0.XXX par la vraie IP${NC}"
else
    echo -e "${YELLOW}configs/config.yaml existe deja, skip${NC}"
fi

# 6. Creer configs/gmail-credentials.json (placeholder, doit etre telecharge depuis GCP)
if [ ! -f "configs/gmail-credentials.json" ]; then
    cat > configs/gmail-credentials.json.example <<'EOF'
{
  "installed": {
    "client_id": "VOTRE_CLIENT_ID.apps.googleusercontent.com",
    "project_id": "agent-mail-24-7",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    "client_secret": "VOTRE_CLIENT_SECRET",
    "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"]
  }
}
EOF
    echo -e "${YELLOW}configs/gmail-credentials.json.example cree (placeholder)${NC}"
    echo -e "${RED}>>> TELECHARGER le vrai credentials.json depuis Google Cloud Console${NC}"
    echo -e "${RED}    et le placer dans configs/gmail-credentials.json${NC}"
fi

echo ""
echo -e "${GREEN}=== Bootstrap termine ===${NC}"
echo ""
echo "Prochaines etapes :"
echo "  1. Editer configs/.env (DB password, OAuth client_id/secret)"
echo "  2. Editer configs/config.yaml (remplacer 10.0.0.XXX par votre IP)"
echo "  3. Creer la DB : psql -h <ip> -U postgres -c 'CREATE DATABASE email_learner;'"
echo "  4. Lancer les migrations : make migrate"
echo "  5. Configurer OAuth : make setup-oauth (puis suivre le guide)"
echo "  6. Lancer le daemon : make run"
echo ""
echo "Ou utiliser le Makefile pour les raccourcis : make help"
