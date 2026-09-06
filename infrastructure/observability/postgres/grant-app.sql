\set ON_ERROR_STOP on
SET ROLE azurpilot_owner;
BEGIN;

GRANT USAGE ON SCHEMA azurpilot TO azurpilot_app;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON ALL TABLES IN SCHEMA azurpilot
    TO azurpilot_app;
GRANT USAGE, SELECT, UPDATE
    ON ALL SEQUENCES IN SCHEMA azurpilot
    TO azurpilot_app;
SELECT format(
    'GRANT SELECT ON TABLE %I.%I TO azurpilot_app',
    'public',
    'alembic_version'
)
WHERE to_regclass('public.alembic_version') IS NOT NULL
\gexec

ALTER DEFAULT PRIVILEGES FOR ROLE azurpilot_owner IN SCHEMA azurpilot
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO azurpilot_app;
ALTER DEFAULT PRIVILEGES FOR ROLE azurpilot_owner IN SCHEMA azurpilot
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO azurpilot_app;

COMMIT;
