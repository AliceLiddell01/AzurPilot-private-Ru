#!/usr/bin/env bash
set -Eeuo pipefail
set +x

psql=(
    psql
    --no-psqlrc
    --username "$POSTGRES_USER"
    --dbname "$POSTGRES_DB"
    --set ON_ERROR_STOP=1
)

hba_file="$("${psql[@]}" --tuples-only --no-align --quiet --command 'SHOW hba_file')"
hba_file="$(printf '%s' "$hba_file" | tr -d '\r\n')"
hba_temporary="${hba_file}.azurpilot.tmp"

cleanup() {
    if [[ -n "${hba_temporary:-}" ]]; then
        rm -f -- "$hba_temporary"
    fi
}
trap cleanup EXIT

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

unset hba_file
