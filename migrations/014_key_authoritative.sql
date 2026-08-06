-- ============================================================================
-- MIGRATION 014 — AUTHORITATIVE KEYS (resolution policy)
-- Applies ON TOP of 013. Additive only. Keep models.py (ResolutionPolicy) and
-- scoring_tiered.py (Tier 0) in lock-step (same commit).
-- ----------------------------------------------------------------------------
-- WHAT THIS FIXES. Tier 0 resolves a mention when a STRONG extracted key
-- already sits on an entity. When the key is present but UNSEEN, it falls
-- through to Tier 1's name/embedding similarity. For a corpus whose keys are
-- sparse and fallible (an email an LLM read off a signature block) that is
-- right: a key nobody has seen is weak evidence, and the fuzzy tier is a
-- second opinion worth having.
--
-- For a corpus whose keys are COMPLETE and AUTHORITATIVE it is exactly wrong,
-- and the first real statute ingest (26 U.S.C. § 63, 2026-08-04) showed how
-- wrong. Every provision carries a USLM identifier that is globally unique by
-- construction, and sibling provisions have near-identical citation strings by
-- construction. Measured with this repo's own name_similarity():
--
--     26 U.S.C. § 63(a)          vs  26 U.S.C. § 63             0.9032
--     26 U.S.C. § 151            vs  26 U.S.C. § 152            0.9333
--     26 U.S.C. § 63(c)(4)(A)    vs  26 U.S.C. § 63(c)(4)(B)    0.9565
--     26 U.S.C. § 63             vs  26 U.S.C. § 163            0.9655
--     26 U.S.C. § 63(c)(2)(A)(i) vs  26 U.S.C. § 63(c)(2)(A)(ii) 0.9811
--
-- Every pair above is two DIFFERENT provisions. There is no threshold that
-- separates same from different here, because the distinguishing information
-- is one or two characters inside a long identical string. Tuning cannot fix
-- a signal that carries no signal. Of 73 provision mentions in that ingest, 4
-- resolved and 69 went to review, and any band tight enough to fix that would
-- have merged § 63 into § 163 — a silent wrong merge in a legal corpus.
--
-- THE FLAG. keys_are_authoritative says: for this entity type, a strong key
-- decides identity, and similarity never gets a vote. An unseen key then means
-- a NEW entity rather than an invitation to fuzzy-match. Two identical keys
-- still mean the same entity; two conflicting ones still go to review.
--
-- DEFAULT false, so every type already in resolution_policy behaves exactly as
-- it did — Person and Organization keep the fuzzy second opinion their
-- LLM-extracted keys need. This is a per-type OPT-IN, declared as data by the
-- corpus that has earned it, not a new global assumption.
-- ============================================================================

BEGIN;

ALTER TABLE resolution_policy
    ADD COLUMN keys_are_authoritative BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN resolution_policy.keys_are_authoritative IS
    'Strong extracted keys decide identity for this type: an unseen key means '
    'a new entity, never a similarity match. Opt-in; only set it for a corpus '
    'whose keys are complete and externally guaranteed unique.';

-- Provision (the USLM statute plugin's only entity type). Its key,
-- uslm_identifier, is a globally unique published path — a real deterministic
-- key, not a name that merely looks unique.
--
-- The bands below apply ONLY to a Provision mention arriving with no
-- identifier at all, which the parser never emits. That case is a defect
-- upstream, so it is banded to land in review rather than to guess: t_high
-- sits above the highest measured similarity between two DIFFERENT provisions
-- (0.9811), which makes a name-only auto-merge unreachable by construction.
-- precision_target is NULL on purpose — a deterministic key's precision is
-- structural, so there is no threshold here for the ER benchmark to calibrate.
INSERT INTO resolution_policy
    (entity_type, t_high, t_low, precision_target, requires_corroboration,
     auto_merge_allowed, keys_are_authoritative, notes)
VALUES
    ('Provision', 0.995, 0.50, NULL, false, true, true,
     'USLM identifiers are authoritative: Tier 0 decides, similarity never '
     'does. Bands cover only the keyless anomaly and are set so a name-only '
     'auto-merge cannot happen (t_high > 0.9811, the highest measured '
     'similarity between two different provisions).')
ON CONFLICT (entity_type) DO UPDATE
    SET keys_are_authoritative = EXCLUDED.keys_are_authoritative,
        auto_merge_allowed     = EXCLUDED.auto_merge_allowed,
        t_high                 = EXCLUDED.t_high,
        t_low                  = EXCLUDED.t_low,
        notes                  = EXCLUDED.notes,
        updated_at             = now();

COMMIT;
