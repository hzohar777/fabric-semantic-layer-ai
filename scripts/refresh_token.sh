#!/usr/bin/env bash
# refresh_token.sh — Refresh the Power BI access token on Azure Container Apps
#
# The Power BI REST API access token expires every hour.
# Run this script before demos or set a cron job to refresh automatically.
#
# Requirements: Azure CLI (az) — logged in and targeting the right subscription
#
# Usage:
#   ./scripts/refresh_token.sh
#   ./scripts/refresh_token.sh --resource-group my-rg --app-name my-mcp-server

set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-fabric-semantic-layer-ai}"
APP_NAME="${APP_NAME:-fabric-mcp-server}"

# Allow CLI overrides
while [[ $# -gt 0 ]]; do
  case $1 in
    --resource-group) RESOURCE_GROUP="$2"; shift 2 ;;
    --app-name)       APP_NAME="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

echo "🔄  Fetching fresh Power BI access token..."
PBI_TOKEN=$(az account get-access-token \
  --resource https://analysis.windows.net/powerbi/api \
  --query accessToken -o tsv)

echo "🚀  Updating ${APP_NAME} in ${RESOURCE_GROUP}..."
az containerapp update \
  --name "${APP_NAME}" \
  --resource-group "${RESOURCE_GROUP}" \
  --set-env-vars "POWERBI_ACCESS_TOKEN=${PBI_TOKEN}" \
  --query "properties.provisioningState" -o tsv

echo "✅  Token refreshed successfully."
