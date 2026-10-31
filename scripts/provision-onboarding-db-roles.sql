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

-- S0 (review R11): runtime least-privilege matrix for the core tables.
-- Executable form of docs/IAP-TENANT-ONBOARDING-DB-ROLES.md §S0. Runs as a
-- privileged principal outside Alembic; login secrets are assigned through
-- the approved secret procedure, never here. All statements are idempotent
-- (IF EXISTS / GRANT re-issue / DO guards) for safe dry-run on disposable DBs.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'iap_tenant_directory') THEN
        CREATE ROLE iap_tenant_directory NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
END $$;

-- Ownership transfer (IF EXISTS): iap_migrator owns the core tables it
-- migrates, mirroring the onboarding transfer above. DDL under a forced-RLS
-- table is unaffected by policies; data backfills need a per-tenant loop.
DO $$ DECLARE t text; BEGIN
    FOREACH t IN ARRAY ARRAY[
        'investigations', 'investigation_facts', 'investigation_checkpoints',
        'investigation_transitions', 'evidence', 'evidence_relationships',
        'timeline_events', 'hypotheses', 'findings', 'investigation_conclusions',
        'investigation_contradictions', 'log_exemplars', 'evidence_bank_interest',
        'application_profiles', 'idempotency_keys', 'outbox_events',
        'action_executions', 'knowledge_artifacts', 'investigation_sessions',
        'code_issue_index', 'background_jobs', 'quota_counters',
        'input_requirements', 'input_fulfillments', 'finding_clusters',
        'finding_cluster_assignments', 'finding_embeddings', 'chat_sessions',
        'chat_messages', 'reference_document_chunks', 'reference_index_generations',
        'batch_intake_records', 'proof_derivations', 'proof_publication_commits'
    ] LOOP
        IF to_regclass('public.' || t) IS NOT NULL THEN
            EXECUTE format('ALTER TABLE public.%I OWNER TO iap_migrator', t);
        END IF;
    END LOOP;
END $$;

-- Belt-and-braces: no PUBLIC access on core tables.
DO $$ DECLARE t text; BEGIN
    FOREACH t IN ARRAY ARRAY[
        'investigations', 'investigation_facts', 'investigation_checkpoints',
        'investigation_transitions', 'evidence', 'evidence_relationships',
        'timeline_events', 'hypotheses', 'findings', 'investigation_conclusions',
        'investigation_contradictions', 'log_exemplars', 'evidence_bank_interest',
        'application_profiles', 'idempotency_keys', 'outbox_events',
        'action_executions', 'knowledge_artifacts', 'investigation_sessions',
        'code_issue_index', 'background_jobs', 'quota_counters',
        'input_requirements', 'input_fulfillments', 'finding_clusters',
        'finding_cluster_assignments', 'finding_embeddings', 'chat_sessions',
        'chat_messages', 'reference_document_chunks', 'reference_index_generations',
        'batch_intake_records', 'proof_derivations', 'proof_publication_commits'
    ] LOOP
        IF to_regclass('public.' || t) IS NOT NULL THEN
            EXECUTE format('REVOKE ALL ON TABLE public.%I FROM PUBLIC', t);
        END IF;
    END LOOP;
END $$;

-- Full-verb core tables (app + worker): SELECT, INSERT, UPDATE, DELETE.
GRANT SELECT, INSERT, UPDATE, DELETE ON
    investigations, evidence, timeline_events, investigation_conclusions,
    hypotheses, idempotency_keys, background_jobs, batch_intake_records,
    reference_document_chunks, reference_index_generations, proof_derivations,
    proof_publication_commits, input_requirements, quota_counters,
    knowledge_artifacts
    TO iap_app, iap_worker;

-- Append-mostly tables (app + worker): SELECT, INSERT, UPDATE (no DELETE path).
GRANT SELECT, INSERT, UPDATE ON
    investigation_contradictions, finding_clusters, finding_cluster_assignments,
    finding_embeddings, outbox_events
    TO iap_app, iap_worker;

-- Insert-oriented tables (app + worker): SELECT, INSERT.
GRANT SELECT, INSERT ON
    investigation_facts, investigation_checkpoints, investigation_transitions,
    evidence_relationships, findings, action_executions, input_fulfillments,
    investigation_sessions, code_issue_index, log_exemplars,
    evidence_bank_interest
    TO iap_app, iap_worker;

-- Profiles: API manages revisions; worker reads for gating.
GRANT SELECT, INSERT, DELETE ON application_profiles TO iap_app;
GRANT SELECT ON application_profiles TO iap_worker;

-- Chat: API/chat service only; the worker has no access path.
GRANT SELECT, INSERT, DELETE ON chat_sessions, chat_messages TO iap_app;

-- Sequences: usage for serial-backed ids (migrator-owned going forward).
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO iap_app, iap_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE iap_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO iap_app, iap_worker;
