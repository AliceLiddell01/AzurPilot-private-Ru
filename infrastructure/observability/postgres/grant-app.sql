SET ROLE azurpilot_owner;
BEGIN;

GRANT USAGE ON SCHEMA azurpilot TO azurpilot_app;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON ALL TABLES IN SCHEMA azurpilot
    TO azurpilot_app;
GRANT USAGE, SELECT, UPDATE
    ON ALL SEQUENCES IN SCHEMA azurpilot
    TO azurpilot_app;
GRANT SELECT ON TABLE public.alembic_version TO azurpilot_app;

ALTER DEFAULT PRIVILEGES FOR ROLE azurpilot_owner IN SCHEMA azurpilot
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO azurpilot_app;
ALTER DEFAULT PRIVILEGES FOR ROLE azurpilot_owner IN SCHEMA azurpilot
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO azurpilot_app;

COMMIT;
