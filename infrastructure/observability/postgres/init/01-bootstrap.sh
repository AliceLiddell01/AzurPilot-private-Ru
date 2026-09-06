#!/usr/bin/env bash
set -Eeuo pipefail
set +x

read_secret() {
    local path="$1"
    local value
    if [[ ! -r "$path" ]]; then
        printf '%s\n' "Секрет PostgreSQL отсутствует." >&2
        exit 1
    fi
    value="$(tr -d '\r\n' < "$path")"
    if [[ -z "$value" ]]; then
        printf '%s\n' "Секрет PostgreSQL пуст." >&2
        exit 1
    fi
    printf '%s' "$value"
}

app_password="$(read_secret /run/secrets/postgres_app_password)"
migrator_password="$(read_secret /run/secrets/postgres_migrator_password)"
app_sql="$(printf '%s' "$app_password" | sed "s/'/''/g")"
migrator_sql="$(printf '%s' "$migrator_password" | sed "s/'/''/g")"

psql=(
    psql
    --no-psqlrc
    --username "$POSTGRES_USER"
    --dbname "$POSTGRES_DB"
    --set ON_ERROR_STOP=1
)

"${psql[@]}" <<SQL
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'azurpilot_owner') THEN
        CREATE ROLE azurpilot_owner
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOINHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'azurpilot_migrator') THEN
        CREATE ROLE azurpilot_migrator
            LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            INHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'azurpilot_app') THEN
        CREATE ROLE azurpilot_app
            LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            INHERIT NOREPLICATION NOBYPASSRLS;
    END IF;
END
\$\$;

SET log_statement = 'none';
ALTER ROLE azurpilot_app
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    INHERIT NOREPLICATION NOBYPASSRLS
    PASSWORD '${app_sql}';
ALTER ROLE azurpilot_migrator
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    INHERIT NOREPLICATION NOBYPASSRLS
    PASSWORD '${migrator_sql}';
RESET log_statement;
GRANT azurpilot_owner TO azurpilot_migrator;
SELECT format('ALTER DATABASE %I OWNER TO azurpilot_owner', current_database()) \gexec
CREATE SCHEMA IF NOT EXISTS azurpilot AUTHORIZATION azurpilot_owner;
SELECT format(
    'GRANT CONNECT ON DATABASE %I TO azurpilot_app, azurpilot_migrator',
    current_database()
) \gexec
GRANT USAGE ON SCHEMA azurpilot TO azurpilot_app, azurpilot_migrator;
ALTER DEFAULT PRIVILEGES FOR ROLE azurpilot_owner IN SCHEMA azurpilot
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO azurpilot_app;
ALTER DEFAULT PRIVILEGES FOR ROLE azurpilot_owner IN SCHEMA azurpilot
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO azurpilot_app;
SELECT format(
    'ALTER ROLE azurpilot_migrator IN DATABASE %I SET ROLE TO azurpilot_owner',
    current_database()
) \gexec
SQL

hba_file="$("${psql[@]}" --tuples-only --no-align --quiet --command 'SHOW hba_file')"
hba_file="$(printf '%s' "$hba_file" | tr -d '\r\n')"
hba_temporary="${hba_file}.azurpilot.tmp"

awk '
    BEGIN { replaced = 0 }
    /^[[:space:]]*local[[:space:]]+/ {
        if (replaced == 0) {
            print "local all postgres peer"
            print "local all all scram-sha-256"
            replaced = 1
        }
        next
    }
    { print }
    END {
        if (replaced == 0) {
            print "local all postgres peer"
            print "local all all scram-sha-256"
        }
    }
' "$hba_file" > "$hba_temporary"
chmod --reference="$hba_file" -- "$hba_temporary"
mv -- "$hba_temporary" "$hba_file"
"${psql[@]}" --command 'SELECT pg_reload_conf()' >/dev/null

unset app_password migrator_password app_sql migrator_sql hba_file hba_temporary
