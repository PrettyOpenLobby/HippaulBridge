-- The FFXI bridge's own state (lsb/ffxi_bridge.py), formerly two JSON files:
-- ffxi_idmap.json on the core's data volume and ffxi_accounts.json on the
-- bridge's state volume. Versions 6001-6999 of the shared schema_migrations
-- table belong to CrystalBridge.

-- One row per LSB character the bridge has paired with a PlayOnline Content
-- ID. The bridge writes it; the FFXI title plugin (lsb/ffxititle.py) reads it
-- inside the core's login and authsess processes.
--
--   charid       LSB's chars.charid
--   content_id   the POL Content ID the client knows the character by
--   name         the FFXI character name, '' until a char list names it
--   world_field  the world identity dword the client was told (0x20 record),
--                0 until the bridge has seen one; the plugin derives one then
--   profile      the content profile tail {world, nation, zone, job,
--                joblevel, race}; NULL when the char list has not been seen,
--                which the profile must keep apart from real zeros
--   seen         when the bridge last wrote the row (ISO 8601, UTC)
--
-- content_id is not UNIQUE on purpose. The bridge refuses to bind one id to
-- two charids, but maps written before that refusal can hold such a pair,
-- and an import of one must not fail on it.
CREATE TABLE ffxi_idmap (
    charid      BIGINT PRIMARY KEY,
    content_id  BIGINT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    world_field BIGINT NOT NULL DEFAULT 0,
    profile     JSONB,
    seen        TEXT NOT NULL
);
CREATE INDEX ffxi_idmap_by_content_id ON ffxi_idmap (content_id);

-- A counter that moves on every change to ffxi_idmap, in the same
-- transaction. The title plugin caches the map and re-reads it when this
-- moves, the way it used to re-read the file when its mtime moved.
CREATE TABLE ffxi_idmap_rev (
    id  SMALLINT PRIMARY KEY CHECK (id = 1),
    rev BIGINT NOT NULL
);
INSERT INTO ffxi_idmap_rev (id, rev) VALUES (1, 0);

CREATE FUNCTION ffxi_idmap_bump() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE ffxi_idmap_rev SET rev = rev + 1 WHERE id = 1;
    RETURN NULL;
END
$$;

CREATE TRIGGER ffxi_idmap_changed
    AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON ffxi_idmap
    FOR EACH STATEMENT EXECUTE FUNCTION ffxi_idmap_bump();

-- Which LSB account each POL member has, per LSB world. The password is
-- derived from FFXI_ACCT_SECRET and never stored, so this is a record of
-- which accounts exist, not a credential store.
--
--   world_tag   '' for the primary world, 'alt' for the LSB_ALT_VER world
--               (an LSB account exists only in the instance that made it)
--   login       the LSB account name, pol<member id>
--   created     when LSB confirmed the account (ISO 8601, UTC)
CREATE TABLE ffxi_lsb_account (
    world_tag TEXT NOT NULL DEFAULT '',
    member_id BIGINT NOT NULL,
    login     TEXT NOT NULL,
    created   TEXT NOT NULL,
    PRIMARY KEY (world_tag, member_id)
);
