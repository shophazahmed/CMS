#!/bin/bash
# Runs once, on first boot of an empty Postgres volume.
# POSTGRES_DB (social_hub) holds the CMS/app tables; n8n and NocoDB each get
# their own database for internal metadata so they never collide.
set -euo pipefail
for db in n8n nocodb; do
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    SELECT 'CREATE DATABASE $db OWNER "$POSTGRES_USER"'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$db')\gexec
SQL
done
