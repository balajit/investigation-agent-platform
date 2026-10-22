-- Run as a PostgreSQL administrator outside Alembic. This script provisions
-- role attributes only; an approved secret manager or secure interactive
-- procedure must assign login secrets. Never commit passwords or pass them on
-- a process command line.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'iap_migrator') THEN
        CREATE ROLE iap_migrator LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'iap_app') THEN
        CREATE ROLE iap_app LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'iap_worker') THEN
        CREATE ROLE iap_worker LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'iap_registry_admin') THEN
        CREATE ROLE iap_registry_admin LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
END $$;

-- Runtime roles must not inherit migration privileges. Object-level DML is
-- granted narrowly by migration 024 after the roles exist.
REVOKE iap_migrator FROM iap_app, iap_worker, iap_registry_admin;
REVOKE iap_app FROM iap_migrator, iap_worker, iap_registry_admin;
REVOKE iap_worker FROM iap_migrator, iap_app, iap_registry_admin;
REVOKE iap_registry_admin FROM iap_migrator, iap_app, iap_worker;

-- The schema owner/migration role is assigned by deployment provisioning,
-- not this repository script. Runtime roles receive no schema CREATE grant.
REVOKE CREATE ON SCHEMA public FROM iap_app, iap_worker, iap_registry_admin;
GRANT USAGE, CREATE ON SCHEMA public TO iap_migrator;

-- Transfer only the pre-existing objects Phase 0R must alter. The privileged
-- provisioning principal must own these relations or be authorized to change
-- ownership. Future onboarding objects are created by iap_migrator.
ALTER TABLE IF EXISTS public.background_jobs OWNER TO iap_migrator;
ALTER TABLE IF EXISTS public.tenant_registry OWNER TO iap_migrator;
ALTER TABLE IF EXISTS public.onboarding_requests OWNER TO iap_migrator;
ALTER TABLE IF EXISTS public.onboarding_dispatches OWNER TO iap_migrator;

-- Revision 023 granted its migration CURRENT_USER. Remove any such legacy
-- runtime grant explicitly before provisioning the new app/worker roles.
DO $$ BEGIN
    IF to_regclass('public.tenant_registry') IS NOT NULL THEN
        REVOKE ALL ON TABLE public.tenant_registry FROM iap_app, iap_worker, iap_registry_admin;
    END IF;
    IF to_regclass('public.onboarding_requests') IS NOT NULL THEN
        REVOKE ALL ON TABLE public.onboarding_requests FROM iap_app, iap_worker, iap_registry_admin;
    END IF;
    IF to_regclass('public.onboarding_dispatches') IS NOT NULL THEN
        REVOKE ALL ON TABLE public.onboarding_dispatches FROM iap_app, iap_worker, iap_registry_admin;
    END IF;
END $$;
