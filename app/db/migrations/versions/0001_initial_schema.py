"""initial covenant schema

Revision ID: 0001
Revises:
Create Date: 2026-07-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ---------------------------------------------------------------- source
    op.execute(f"""
    CREATE TABLE {SCHEMA}.source (
      source_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      source_type       TEXT NOT NULL CHECK (source_type IN (
                          'pdf_document','textcache_ocr','vision_ocr_fable5','vision_ocr_opus',
                          'gis_api','recorder_portal','recorder_api','assessor_api',
                          'estimate_derivation','manual_entry')),
      reference         TEXT NOT NULL,
      engine            TEXT,
      retrieved_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
      is_estimated      BOOLEAN NOT NULL DEFAULT false,
      confidence        NUMERIC(4,3) CHECK (confidence BETWEEN 0 AND 1),
      raw_response_path TEXT
    )
    """)

    # ----------------------------------------------------------------- state
    op.execute(f"""
    CREATE TABLE {SCHEMA}.state (
      state_code                  CHAR(2) PRIMARY KEY,
      state_name                  TEXT NOT NULL UNIQUE,
      is_disclosure_state         BOOLEAN,
      ptf_covenant_status          TEXT NOT NULL DEFAULT 'unknown'
                                    CHECK (ptf_covenant_status IN ('enforceable','restricted','banned','unknown')),
      ptf_status_effective_date    DATE,
      ptf_status_statute           TEXT,
      ptf_status_notes             TEXT,
      statute_of_limitations_years NUMERIC(4,1),
      statute_of_limitations_basis TEXT
                                    CHECK (statute_of_limitations_basis IN
                                      ('transfer_date','due_date','delinquency_date','unknown')),
      statute_of_limitations_notes TEXT,
      source_id                    BIGINT REFERENCES {SCHEMA}.source(source_id)
    )
    """)

    # ---------------------------------------------------------------- county
    op.execute(f"""
    CREATE TABLE {SCHEMA}.county (
      county_fips  CHAR(5) PRIMARY KEY,
      state_code   CHAR(2) NOT NULL REFERENCES {SCHEMA}.state(state_code),
      county_name  TEXT NOT NULL,
      UNIQUE (state_code, county_name)
    )
    """)

    op.execute(f"""
    CREATE TABLE {SCHEMA}.county_gis_registry (
      county_fips      CHAR(5) PRIMARY KEY REFERENCES {SCHEMA}.county(county_fips),
      base_url         TEXT NOT NULL,
      service_type     TEXT NOT NULL DEFAULT 'arcgis_rest',
      field_mapping    JSONB NOT NULL,
      quirks           JSONB,
      status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','broken','needs_review')),
      discovered_at    TIMESTAMPTZ,
      last_verified_at TIMESTAMPTZ
    )
    """)

    op.execute(f"""
    CREATE TABLE {SCHEMA}.county_recorder_registry (
      county_fips      CHAR(5) PRIMARY KEY REFERENCES {SCHEMA}.county(county_fips),
      access_tier      TEXT NOT NULL CHECK (access_tier IN
                         ('api_index','portal_playwright','captcha_paywall','offline')),
      base_url         TEXT,
      auth_notes       TEXT,
      workers_allowed  SMALLINT NOT NULL DEFAULT 1,
      quirks           JSONB,
      status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','broken','needs_review')),
      discovered_at    TIMESTAMPTZ,
      last_verified_at TIMESTAMPTZ
    )
    """)

    # --------------------------------------------------------------- contact
    op.execute(f"""
    CREATE TABLE {SCHEMA}.contact (
      contact_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      name_raw        TEXT NOT NULL,
      name_normalized TEXT,
      contact_type      TEXT CHECK (contact_type IN
                        ('individual','entity','trust','government','title_company','unknown')),
      mailing_address TEXT,
      phone           TEXT,
      email           TEXT,
      source_id       BIGINT REFERENCES {SCHEMA}.source(source_id)
    )
    """)

    # -------------------------------------------------- exemption_category
    op.execute(f"""
    CREATE TABLE {SCHEMA}.exemption_category (
      category_code  TEXT PRIMARY KEY,
      label          TEXT NOT NULL,
      description    TEXT NOT NULL
    )
    """)

    # -------------------------------------------------- covenant_template
    op.execute(f"""
    CREATE TABLE {SCHEMA}.covenant_template (
      template_version_id       TEXT PRIMARY KEY,
      status                    TEXT NOT NULL CHECK (status IN ('version','review','unreadable')),
      doc_count                 INTEGER,
      unpaid_interest_percent   NUMERIC(5,2),
      unpaid_interest_source    TEXT,
      sample_covid              INTEGER,
      standard_fee_percent      NUMERIC(5,2),
      trustee_fee_percent       NUMERIC(5,2),
      escrow_reserve_percent    NUMERIC(5,2),
      closing_agent_fee_percent NUMERIC(5,2),
      closing_agent_fee_minimum NUMERIC(10,2)
    )
    """)

    op.execute(f"""
    CREATE TABLE {SCHEMA}.covenant_template_exemption (
      template_version_id                     TEXT NOT NULL REFERENCES {SCHEMA}.covenant_template(template_version_id),
      category_code                            TEXT NOT NULL REFERENCES {SCHEMA}.exemption_category(category_code),
      clause_reference                         TEXT,
      cutoff_date                              DATE,
      cutoff_basis                             TEXT CHECK (cutoff_basis IN ('fixed_date','recording_date')),
      controlling_interest_threshold_percent   NUMERIC(5,2),
      needs_review                             BOOLEAN NOT NULL DEFAULT false,
      review_notes                             TEXT,
      PRIMARY KEY (template_version_id, category_code)
    )
    """)

    # -------------------------------------------------------------- covenant
    op.execute(f"""
    CREATE TABLE {SCHEMA}.covenant (
      covid                     INTEGER PRIMARY KEY,
      county_fips               CHAR(5) NOT NULL REFERENCES {SCHEMA}.county(county_fips),
      declarant_raw             TEXT,
      declarant_contact_id        BIGINT REFERENCES {SCHEMA}.contact(contact_id),
      fee_percent               NUMERIC(5,2),
      term_description          TEXT,
      recording_instrument      TEXT,
      recording_date            DATE,
      book                      TEXT,
      page                      TEXT,
      template_version_id       TEXT REFERENCES {SCHEMA}.covenant_template(template_version_id),
      stated_acreage            NUMERIC(12,3),
      legal_description_raw     TEXT,
      legal_description_type    TEXT CHECK (legal_description_type IN
                                  ('texas_abstract','plss','metes_bounds','unknown')),
      legal_description_parsed  JSONB,
      exemptions_raw            TEXT,
      fee_due_days              INTEGER,
      status                    TEXT NOT NULL DEFAULT 'ingested' CHECK (status IN
                                  ('ingested','parsed','gis_classified','reconciled',
                                   'title_in_progress','done','needs_review')),
      review_reason             TEXT,
      source_id                 BIGINT REFERENCES {SCHEMA}.source(source_id),
      created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """)

    op.execute(f"""
    CREATE TABLE {SCHEMA}.covenant_document (
      relpath       TEXT PRIMARY KEY,
      covid         INTEGER NOT NULL REFERENCES {SCHEMA}.covenant(covid),
      doc_type      TEXT NOT NULL CHECK (doc_type IN ('original','amendment')),
      pages         INTEGER,
      ocr_engine    TEXT CHECK (ocr_engine IN ('tesseract','fable5_vision','opus_vision','human')),
      vocab_score   NUMERIC(5,4),
      confidence    NUMERIC(4,3),
      source_id     BIGINT REFERENCES {SCHEMA}.source(source_id)
    )
    """)

    # ---------------------------------------------------------------- tract
    op.execute(f"""
    CREATE TABLE {SCHEMA}.tract (
      covid                  INTEGER NOT NULL REFERENCES {SCHEMA}.covenant(covid),
      tract_no                SMALLINT NOT NULL,
      geom                    geometry(MultiPolygon,4326) NOT NULL,
      residual_geom           geometry(MultiPolygon,4326),
      classified_acreage      NUMERIC(12,3),
      unaccounted_acreage     NUMERIC(12,3),
      reconciliation_status   TEXT NOT NULL DEFAULT 'pending' CHECK (reconciliation_status IN
                                ('reconciled','unaccounted_area','over_classified','pending')),
      source_id               BIGINT REFERENCES {SCHEMA}.source(source_id),
      updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
      PRIMARY KEY (covid, tract_no)
    )
    """)
    op.execute(f"CREATE INDEX tract_geom_gix ON {SCHEMA}.tract USING GIST (geom)")
    op.execute(f"CREATE INDEX tract_residual_gix ON {SCHEMA}.tract USING GIST (residual_geom)")

    # --------------------------------------------------------------- parcel
    op.execute(f"""
    CREATE TABLE {SCHEMA}.parcel (
      county_fips      CHAR(5) NOT NULL REFERENCES {SCHEMA}.county(county_fips),
      apn              TEXT NOT NULL,
      owner_name_raw   TEXT,
      current_contact_id BIGINT REFERENCES {SCHEMA}.contact(contact_id),
      situs_address    TEXT,
      city             TEXT,
      zip_code         TEXT,
      acreage          NUMERIC(12,3),
      geom             geometry(MultiPolygon,4326) NOT NULL,
      last_synced_at   TIMESTAMPTZ,
      source_id        BIGINT REFERENCES {SCHEMA}.source(source_id),
      PRIMARY KEY (county_fips, apn)
    )
    """)
    op.execute(f"CREATE INDEX parcel_geom_gix ON {SCHEMA}.parcel USING GIST (geom)")

    # ---------------------------------------------------------- monitor_run
    op.execute(f"""
    CREATE TABLE {SCHEMA}.monitor_run (
      covid                    INTEGER NOT NULL REFERENCES {SCHEMA}.covenant(covid),
      run_seq                  SMALLINT NOT NULL,
      run_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
      run_type                 TEXT NOT NULL CHECK (run_type IN ('initial','scheduled','manual')),
      new_parcels_found        INTEGER,
      residual_acreage_before  NUMERIC(12,3),
      residual_acreage_after   NUMERIC(12,3),
      status                   TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','error')),
      PRIMARY KEY (covid, run_seq)
    )
    """)

    # ----------------------------------------------------- parcel_covenant
    op.execute(f"""
    CREATE TABLE {SCHEMA}.parcel_covenant (
      county_fips      CHAR(5) NOT NULL,
      apn              TEXT NOT NULL,
      covid            INTEGER NOT NULL,
      tract_no         SMALLINT NOT NULL,
      run_seq          SMALLINT NOT NULL,
      classification   TEXT NOT NULL CHECK (classification IN ('interior','boundary','excluded')),
      overlap_fraction NUMERIC(5,4),
      confidence       NUMERIC(4,3),
      rationale        TEXT,
      classified_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
      PRIMARY KEY (county_fips, apn, covid, tract_no, run_seq),
      FOREIGN KEY (county_fips, apn) REFERENCES {SCHEMA}.parcel(county_fips, apn),
      FOREIGN KEY (covid, tract_no)  REFERENCES {SCHEMA}.tract(covid, tract_no),
      FOREIGN KEY (covid, run_seq)   REFERENCES {SCHEMA}.monitor_run(covid, run_seq)
    )
    """)

    # ------------------------------------------------------- parcel_history
    op.execute(f"""
    CREATE TABLE {SCHEMA}.parcel_history (
      county_fips               CHAR(5) NOT NULL,
      apn                       TEXT NOT NULL,
      captured_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
      owner_name_raw            TEXT,
      acreage                   NUMERIC(12,3),
      geom                      geometry(MultiPolygon,4326),
      change_reason             TEXT NOT NULL CHECK (change_reason IN
                                  ('initial','replat','ownership_change','monitor_diff')),
      superseded_by_county_fips CHAR(5),
      superseded_by_apn         TEXT,
      source_id                 BIGINT REFERENCES {SCHEMA}.source(source_id),
      PRIMARY KEY (county_fips, apn, captured_at),
      FOREIGN KEY (county_fips, apn) REFERENCES {SCHEMA}.parcel(county_fips, apn),
      FOREIGN KEY (superseded_by_county_fips, superseded_by_apn) REFERENCES {SCHEMA}.parcel(county_fips, apn)
    )
    """)

    # -------------------------------------------------------------- transfer
    op.execute(f"""
    CREATE TABLE {SCHEMA}.transfer (
      county_fips              CHAR(5) NOT NULL REFERENCES {SCHEMA}.county(county_fips),
      instrument_number        TEXT NOT NULL,
      instrument_number_type   TEXT NOT NULL DEFAULT 'modern_instrument' CHECK (instrument_number_type IN
                                 ('modern_instrument','book_page','land_patent')),
      covid                    INTEGER NOT NULL,
      tract_no                 SMALLINT NOT NULL,
      parcel_county_fips       CHAR(5),
      parcel_apn               TEXT,
      prior_county_fips        CHAR(5),
      prior_instrument_number  TEXT,
      instrument_type          TEXT,
      recording_date           DATE,
      book                     TEXT,
      page                     TEXT,
      grantor_contact_id         BIGINT REFERENCES {SCHEMA}.contact(contact_id),
      grantee_contact_id         BIGINT REFERENCES {SCHEMA}.contact(contact_id),
      consideration_amount     NUMERIC(14,2),
      legal_description_snapshot TEXT,
      recorder_source_id       BIGINT REFERENCES {SCHEMA}.source(source_id),
      review_flag              BOOLEAN NOT NULL DEFAULT false,
      review_reason            TEXT,
      exemption_category       TEXT REFERENCES {SCHEMA}.exemption_category(category_code),
      exemption_basis          TEXT,
      exemption_confidence     NUMERIC(4,3),
      PRIMARY KEY (county_fips, instrument_number),
      FOREIGN KEY (covid, tract_no) REFERENCES {SCHEMA}.tract(covid, tract_no),
      FOREIGN KEY (parcel_county_fips, parcel_apn) REFERENCES {SCHEMA}.parcel(county_fips, apn),
      FOREIGN KEY (prior_county_fips, prior_instrument_number) REFERENCES {SCHEMA}.transfer(county_fips, instrument_number)
    )
    """)
    op.execute(f"CREATE INDEX transfer_parcel_idx ON {SCHEMA}.transfer(parcel_county_fips, parcel_apn)")
    op.execute(f"CREATE INDEX transfer_prior_idx ON {SCHEMA}.transfer(prior_county_fips, prior_instrument_number)")

    # -------------------------------------------------------- price_estimate
    op.execute(f"""
    CREATE TABLE {SCHEMA}.price_estimate (
      county_fips        CHAR(5) NOT NULL,
      instrument_number  TEXT NOT NULL,
      method             TEXT NOT NULL CHECK (method IN ('deed_of_trust_amount','assessor_market_value','other')),
      estimated_amount   NUMERIC(14,2),
      confidence         NUMERIC(4,3),
      rationale          TEXT,
      is_selected        BOOLEAN NOT NULL DEFAULT false,
      source_id          BIGINT REFERENCES {SCHEMA}.source(source_id),
      PRIMARY KEY (county_fips, instrument_number, method),
      FOREIGN KEY (county_fips, instrument_number) REFERENCES {SCHEMA}.transfer(county_fips, instrument_number)
    )
    """)

    # -------------------------------------------------------- fee_collection
    op.execute(f"""
    CREATE TABLE {SCHEMA}.fee_collection (
      county_fips          CHAR(5) NOT NULL,
      instrument_number    TEXT NOT NULL,
      collection_seq       SMALLINT NOT NULL DEFAULT 1,
      fee_percent_applied  NUMERIC(5,2) NOT NULL,
      base_amount          NUMERIC(14,2),
      due_date             DATE,
      invoiced_amount       NUMERIC(14,2),
      invoice_date          DATE,
      collected_amount      NUMERIC(14,2),
      collected_date        DATE,
      status                TEXT NOT NULL DEFAULT 'owed' CHECK (status IN
                              ('owed','invoiced','partial','paid','delinquent','lien_filed',
                               'waived','disputed','uncollectible')),
      payer_contact_id         BIGINT REFERENCES {SCHEMA}.contact(contact_id),
      remittance_reference   TEXT,
      legacy_source_ref      TEXT,
      notes                  TEXT,
      source_id              BIGINT REFERENCES {SCHEMA}.source(source_id),
      collectibility_status  TEXT NOT NULL DEFAULT 'collectible' CHECK (collectibility_status IN
                               ('collectible','time_barred','cleared_by_estoppel')),
      collectibility_note        TEXT,
      collectibility_checked_at  TIMESTAMPTZ,
      PRIMARY KEY (county_fips, instrument_number, collection_seq),
      FOREIGN KEY (county_fips, instrument_number) REFERENCES {SCHEMA}.transfer(county_fips, instrument_number)
    )
    """)
    op.execute(f"CREATE INDEX fee_collection_status_idx ON {SCHEMA}.fee_collection(status)")
    op.execute(f"""CREATE INDEX fee_collection_due_idx ON {SCHEMA}.fee_collection(due_date)
                   WHERE status NOT IN ('paid','waived')""")

    # ------------------------------------------------- fee_payoff_statement
    op.execute(f"""
    CREATE TABLE {SCHEMA}.fee_payoff_statement (
      county_fips              CHAR(5) NOT NULL,
      instrument_number        TEXT NOT NULL,
      collection_seq           SMALLINT NOT NULL,
      statement_seq            SMALLINT NOT NULL DEFAULT 1,
      principal_amount         NUMERIC(14,2) NOT NULL,
      interest_rate_annual     NUMERIC(5,2) NOT NULL,
      accrual_start_date       DATE NOT NULL,
      good_through_date        DATE NOT NULL,
      accrued_interest_amount  NUMERIC(14,2) NOT NULL,
      per_diem_amount          NUMERIC(14,2) NOT NULL,
      total_payoff_amount      NUMERIC(14,2) NOT NULL,
      requested_by_contact_id    BIGINT REFERENCES {SCHEMA}.contact(contact_id),
      generated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
      source_id                BIGINT REFERENCES {SCHEMA}.source(source_id),
      PRIMARY KEY (county_fips, instrument_number, collection_seq, statement_seq),
      FOREIGN KEY (county_fips, instrument_number, collection_seq)
        REFERENCES {SCHEMA}.fee_collection(county_fips, instrument_number, collection_seq)
    )
    """)
    op.execute(f"CREATE INDEX fee_payoff_statement_generated_idx ON {SCHEMA}.fee_payoff_statement(generated_at)")

    # ----------------------------------------------------- estoppel_certificate
    op.execute(f"""
    CREATE TABLE {SCHEMA}.estoppel_certificate (
      county_fips             CHAR(5) NOT NULL,
      instrument_number       TEXT NOT NULL,
      covid                   INTEGER NOT NULL REFERENCES {SCHEMA}.covenant(covid),
      parcel_county_fips      CHAR(5) NOT NULL,
      parcel_apn              TEXT NOT NULL,
      certificate_type        TEXT NOT NULL CHECK (certificate_type IN
                                ('estoppel_certificate','substitute_estoppel_certificate')),
      recording_date          DATE NOT NULL,
      issued_by_contact_id      BIGINT REFERENCES {SCHEMA}.contact(contact_id),
      source_id               BIGINT REFERENCES {SCHEMA}.source(source_id),
      PRIMARY KEY (county_fips, instrument_number),
      FOREIGN KEY (parcel_county_fips, parcel_apn) REFERENCES {SCHEMA}.parcel(county_fips, apn)
    )
    """)
    op.execute(f"""CREATE INDEX estoppel_certificate_parcel_idx
                   ON {SCHEMA}.estoppel_certificate(parcel_county_fips, parcel_apn, recording_date)""")

    # --------------------------------------------------------- covenant_trustee
    op.execute(f"""
    CREATE TABLE {SCHEMA}.covenant_trustee (
      covid           INTEGER NOT NULL REFERENCES {SCHEMA}.covenant(covid),
      effective_date  DATE NOT NULL,
      contact_id        BIGINT NOT NULL REFERENCES {SCHEMA}.contact(contact_id),
      source_id       BIGINT REFERENCES {SCHEMA}.source(source_id),
      PRIMARY KEY (covid, effective_date)
    )
    """)

    # ----------------------------------------------------- covenant_beneficiary
    op.execute(f"""
    CREATE TABLE {SCHEMA}.covenant_beneficiary (
      covid                INTEGER NOT NULL REFERENCES {SCHEMA}.covenant(covid),
      beneficiary_seq      SMALLINT NOT NULL,
      effective_date       DATE NOT NULL,
      contact_id             BIGINT NOT NULL REFERENCES {SCHEMA}.contact(contact_id),
      percentage_interest  NUMERIC(6,3) NOT NULL,
      source_id            BIGINT REFERENCES {SCHEMA}.source(source_id),
      PRIMARY KEY (covid, beneficiary_seq, effective_date)
    )
    """)
    op.execute(f"""CREATE INDEX covenant_beneficiary_current_idx
                   ON {SCHEMA}.covenant_beneficiary(covid, effective_date DESC)""")

    # ---------------------------------------------------------------- event
    op.execute(f"""
    CREATE TABLE {SCHEMA}.event (
      event_id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      covid                       INTEGER REFERENCES {SCHEMA}.covenant(covid),
      parcel_county_fips          CHAR(5),
      parcel_apn                  TEXT,
      transfer_county_fips        CHAR(5),
      transfer_instrument_number  TEXT,
      event_type                  TEXT NOT NULL,
      severity                    TEXT NOT NULL DEFAULT 'info' CHECK (severity IN ('info','warning','error')),
      message                     TEXT,
      payload                     JSONB,
      created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
      FOREIGN KEY (parcel_county_fips, parcel_apn) REFERENCES {SCHEMA}.parcel(county_fips, apn),
      FOREIGN KEY (transfer_county_fips, transfer_instrument_number)
        REFERENCES {SCHEMA}.transfer(county_fips, instrument_number)
    )
    """)

    # ----------------------------------------------------------- job_queue
    op.execute(f"""
    CREATE TABLE {SCHEMA}.job_queue (
      job_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      job_type      TEXT NOT NULL,
      covid         INTEGER REFERENCES {SCHEMA}.covenant(covid),
      county_fips   CHAR(5) REFERENCES {SCHEMA}.county(county_fips),
      payload       JSONB,
      status        TEXT NOT NULL DEFAULT 'queued' CHECK (status IN
                      ('queued','in_progress','done','error','needs_review','captcha_pending')),
      priority      SMALLINT NOT NULL DEFAULT 100,
      attempts      SMALLINT NOT NULL DEFAULT 0,
      locked_by     TEXT,
      locked_at     TIMESTAMPTZ,
      available_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
      error_message TEXT,
      created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
      updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """)
    op.execute(f"""CREATE INDEX job_queue_dequeue_idx
                   ON {SCHEMA}.job_queue (status, county_fips, priority, available_at)""")

    # ------------------------------------------------------- captcha_session
    op.execute(f"""
    CREATE TABLE {SCHEMA}.captcha_session (
      session_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      county_fips   CHAR(5) NOT NULL REFERENCES {SCHEMA}.county(county_fips),
      job_id        BIGINT REFERENCES {SCHEMA}.job_queue(job_id),
      status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','solved','timed_out','reused')),
      opened_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
      solved_at     TIMESTAMPTZ,
      expires_at    TIMESTAMPTZ,
      reused_count  INTEGER NOT NULL DEFAULT 0
    )
    """)

    _seed_exemption_categories()
    _seed_covenant_templates()
    _seed_covenant_template_exemptions()


def downgrade() -> None:
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


# ============================================================== seed data

def _seed_exemption_categories() -> None:
    table = sa.table(
        "exemption_category",
        sa.column("category_code", sa.Text),
        sa.column("label", sa.Text),
        sa.column("description", sa.Text),
        schema=SCHEMA,
    )
    rows = [
        ("declarant_sale", "Declarant sale", "Conveyance by the Declarant (e.g. builder/developer's first sale)."),
        ("security_instrument", "Security instrument", "Mortgage/deed of trust securing indebtedness -- not a real conveyance."),
        ("death_probate", "Death / probate", "Resulting from death or legal disability of an Owner, including transfers by will or probate."),
        ("estate_planning", "Estate planning", "Broader carve-out for 'legitimate estate planning purposes' (e.g. transfer to one's own revocable trust) -- only in some templates."),
        ("foreclosure", "Foreclosure", "By or to an Institutional Lender/trustee in connection with judicial or non-judicial foreclosure."),
        ("government_nonprofit", "Government / nonprofit", "By or to a governmental entity/agency or 501(c)(3) entity."),
        ("affiliate_transaction", "Affiliate transaction", "Grantor has a Controlling Interest in the Grantee, or vice versa (commonly a 50%+ ownership test)."),
        ("spousal", "Spousal", "Transfer solely between spouses."),
        ("lineal_descendant", "Lineal descendant", "Transfer to a direct lineal descendant (in addition to spousal, in templates that offer it)."),
        ("court_order", "Court order", "Made by order of a court (bankruptcy, divorce, etc.), generally excluding an order for specific performance."),
        ("trustee_unidentified", "Trustee unidentified", "Where the Trustee cannot be identified by reference to the instrument or public records."),
        ("pre_effective_date", "Pre-effective-date", "Conveyance occurred before the covenant's stated cutoff -- a fixed calendar date in some templates, the covenant's own recording date in others."),
        ("beneficiary_or_trustee", "Beneficiary or Trustee", "Conveyance by a Beneficiary or by the Trustee acting in that capacity (distinct from a homeowner's personal trust)."),
        ("easement_leasehold_grant", "Easement / leasehold / mortgage grant", "Relating to the creation of an easement, leasehold interest, or the granting of a mortgage."),
        ("prohibited_by_law", "Prohibited by law", "Where imposition of the fee is prohibited by applicable law (e.g. a state PTF-covenant ban)."),
        ("other", "Other", "Any exemption category not covered above -- see review_notes."),
    ]
    op.bulk_insert(table, [dict(zip(("category_code", "label", "description"), r)) for r in rows])


def _seed_covenant_templates() -> None:
    table = sa.table(
        "covenant_template",
        sa.column("template_version_id", sa.Text),
        sa.column("status", sa.Text),
        sa.column("doc_count", sa.Integer),
        sa.column("sample_covid", sa.Integer),
        schema=SCHEMA,
    )
    # (template_version_id, status, doc_count, sample_covid) -- from Covenant_Matrix/covenant_matrix.json
    rows = [
        ("V01", "version", 253, 5854), ("V02", "version", 208, 7990), ("V03", "version", 157, 9147),
        ("V04", "version", 118, 4744), ("V05", "version", 92, 2297), ("V06", "version", 33, 6376),
        ("V07", "version", 23, 5858), ("V08", "version", 18, 2664), ("V09", "version", 17, 2088),
        ("V10", "version", 15, 2825), ("V11", "version", 14, 7212), ("V12", "version", 11, 8071),
        ("V13", "version", 8, 8027), ("V14", "version", 7, 5964), ("V15", "version", 7, 4930),
        ("V16", "version", 6, 2298), ("V17", "version", 5, 6334), ("V18", "version", 4, 4420),
        ("R01", "review", 1, 2751), ("R02", "review", 1, 3925), ("R03", "review", 1, 5464),
        ("R04", "review", 1, 5852), ("R05", "review", 1, 5989), ("R06", "review", 1, 5990),
        ("R07", "review", 1, 8084), ("R08", "review", 1, 8319),
        ("U01", "unreadable", 1, 2090), ("U02", "unreadable", 1, 2095), ("U03", "unreadable", 1, 2109),
        ("U04", "unreadable", 1, 2115), ("U05", "unreadable", 1, 2117), ("U06", "unreadable", 1, 2119),
        ("U07", "unreadable", 1, 2120), ("U08", "unreadable", 1, 2126), ("U09", "unreadable", 1, 2332),
        ("U10", "unreadable", 1, 2333), ("U11", "unreadable", 1, 2335), ("U12", "unreadable", 1, 2336),
        ("U13", "unreadable", 1, 2338), ("U14", "unreadable", 1, 2339), ("U15", "unreadable", 1, 2363),
        ("U16", "unreadable", 1, 3193), ("U17", "unreadable", 1, 3639), ("U18", "unreadable", 1, 3697),
        ("U19", "unreadable", 1, 3699), ("U20", "unreadable", 1, 3700), ("U21", "unreadable", 1, 3703),
        ("U22", "unreadable", 1, 3897), ("U23", "unreadable", 1, 4497), ("U24", "unreadable", 1, 4956),
        ("U25", "unreadable", 1, 4974), ("U26", "unreadable", 1, 4976), ("U27", "unreadable", 1, 4981),
        ("U28", "unreadable", 1, 4989), ("U29", "unreadable", 1, 4990), ("U30", "unreadable", 1, 4998),
        ("U31", "unreadable", 1, 5205), ("U32", "unreadable", 1, 5670), ("U33", "unreadable", 1, 5671),
        ("U34", "unreadable", 1, 5672), ("U35", "unreadable", 1, 5673), ("U36", "unreadable", 1, 5991),
        ("U37", "unreadable", 1, 5993), ("U38", "unreadable", 1, 6117), ("U39", "unreadable", 1, 6393),
        ("U40", "unreadable", 1, 6674), ("U41", "unreadable", 1, 6898), ("U42", "unreadable", 1, 7029),
        ("U43", "unreadable", 1, 7030), ("U44", "unreadable", 1, 7031), ("U45", "unreadable", 1, 7296),
        ("U46", "unreadable", 1, 7312), ("U47", "unreadable", 1, 7777), ("U48", "unreadable", 1, 7854),
        ("U49", "unreadable", 1, 8299), ("U50", "unreadable", 1, 8313), ("U51", "unreadable", 1, 9174),
        ("U52", "unreadable", 1, 9175),
    ]
    op.bulk_insert(table, [
        dict(zip(("template_version_id", "status", "doc_count", "sample_covid"), r)) for r in rows
    ])

    # Fields confirmed directly from real per-covenant text (§1, §6, §8, §9-13) -- V01 family, cross-checked
    # against covids 5854/2326/2327. Left NULL for templates not yet individually verified.
    op.execute(f"""
        UPDATE {SCHEMA}.covenant_template
        SET unpaid_interest_percent = 18.00,
            unpaid_interest_source = 'Section 8.l LIEN AND PRIORITY; LIABILITY; COLLECTION',
            standard_fee_percent = 1.00,
            trustee_fee_percent = 3.00,
            escrow_reserve_percent = 5.00,
            closing_agent_fee_percent = 2.00,
            closing_agent_fee_minimum = 100.00
        WHERE template_version_id = 'V01'
    """)


def _seed_covenant_template_exemptions() -> None:
    table = sa.table(
        "covenant_template_exemption",
        sa.column("template_version_id", sa.Text),
        sa.column("category_code", sa.Text),
        sa.column("clause_reference", sa.Text),
        sa.column("cutoff_date", sa.Date),
        sa.column("cutoff_basis", sa.Text),
        sa.column("controlling_interest_threshold_percent", sa.Numeric),
        sa.column("needs_review", sa.Boolean),
        sa.column("review_notes", sa.Text),
        schema=SCHEMA,
    )

    def std_set(letters, cutoff_date=None, cutoff_basis="fixed_date", ci=50.00):
        """The common (a)-(h) skeleton shared by every affiliate/controlling-interest-family template,
        plus the (i)/(j) pre_effective_date row with whatever cutoff this template actually uses."""
        rows = [
            (letters["a"], "declarant_sale", None, None, None, None),
            (letters["b"], "security_instrument", None, None, None, None),
            (letters["c"], "death_probate", None, None, None, None),
            (letters["d"], "foreclosure", None, None, None, None),
            (letters["e"], "government_nonprofit", None, None, None, None),
            (letters["g"], "court_order", None, None, None, None),
            (letters["h"], "trustee_unidentified", None, None, None, None),
        ]
        if "f_affiliate" in letters:
            rows.append((letters["f_affiliate"], "affiliate_transaction", None, None, ci, None))
        if "f_easement" in letters:
            rows.append((letters["f_easement"], "easement_leasehold_grant", None, None, None, None))
        rows.append((letters["cutoff_letter"], "pre_effective_date", cutoff_date, cutoff_basis, None, None))
        return rows

    data = {}  # template_version_id -> list of (clause, category, cutoff_date, cutoff_basis, ci_pct, review_notes)

    # ---- Family 1a: declarant/security/death_probate/foreclosure/gov/AFFILIATE/court/trustee/cutoff(fixed)
    for tvid, cutoff in [("V01", "2013-01-01"), ("V04", "2013-01-01"), ("V07", "2010-12-31"),
                          ("V14", "2013-01-01"), ("V15", "2012-01-01"), ("V17", "2012-01-01")]:
        letters = {"a": "6(a)", "b": "6(b)", "c": "6(c)", "d": "6(d)", "e": "6(e)",
                   "f_affiliate": "6(f)", "g": "6(g)", "h": "6(h)", "cutoff_letter": "6(i)"}
        data[tvid] = std_set(letters, cutoff_date=cutoff, cutoff_basis="fixed_date")

    # V13: Family 1a structure but cutoff = recording_date (dynamic), no prohibited_by_law
    data["V13"] = std_set(
        {"a": "6(a)", "b": "6(b)", "c": "6(c)", "d": "6(d)", "e": "6(e)",
         "f_affiliate": "6(f)", "g": "6(g)", "h": "6(h)", "cutoff_letter": "6(i)"},
        cutoff_date=None, cutoff_basis="recording_date",
    )

    # ---- Family 1b: broader lender language, affidavit requirement, fixed cutoff
    for tvid, cutoff in [("V02", "2012-01-01"), ("V03", "2012-01-01"), ("V12", "2013-01-01")]:
        letters = {"a": "6(a)", "b": "6(b)", "c": "6(c)", "d": "6(d)", "e": "6(e)",
                   "f_affiliate": "6(f)", "g": "6(g)", "h": "6(h)", "cutoff_letter": "6(i)"}
        data[tvid] = std_set(letters, cutoff_date=cutoff, cutoff_basis="fixed_date")

    # ---- V18: Family 1a + prohibited_by_law; needs_review on affiliate clause + cutoff date
    data["V18"] = std_set(
        {"a": "6(a)", "b": "6(b)", "c": "6(c)", "d": "6(d)", "e": "6(e)",
         "f_affiliate": "6(f)", "g": "6(g)", "h": "6(h)", "cutoff_letter": "6(j)"},
        cutoff_date="2012-01-07", cutoff_basis="fixed_date", ci=None,
    )
    data["V18"].append(("6(i)", "prohibited_by_law", None, None, None, None))

    # ---- V06: easement/leasehold variant (no affiliate_transaction despite defining Controlling Interest),
    # cutoff = recording_date, has prohibited_by_law
    data["V06"] = std_set(
        {"a": "6(a)", "b": "6(b)", "c": "6(c)", "d": "6(d)", "e": "6(e)",
         "f_easement": "6(f)", "g": "6(g)", "h": "6(h)", "cutoff_letter": "6(j)"},
        cutoff_date=None, cutoff_basis="recording_date",
    )
    data["V06"].append(("6(i)", "prohibited_by_law", None, None, None, None))

    # ---- V08, V10: estate-planning family (death_probate + estate_planning share clause (c))
    for tvid in ("V08", "V10"):
        rows = std_set(
            {"a": "6(a)", "b": "6(b)", "c": "6(c)", "d": "6(d)", "e": "6(e)",
             "f_easement": "6(f)", "g": "6(g)", "h": "6(h)", "cutoff_letter": "6(j)"},
            cutoff_date=None, cutoff_basis="recording_date",
        )
        rows.append(("6(c)", "estate_planning", None, None, None, None))
        rows.append(("6(i)", "prohibited_by_law", None, None, None, None))
        data[tvid] = rows

    # ---- Spousal family: V05, V11, V16 (identical structure, incl. trustee_unidentified)
    for tvid in ("V05", "V11", "V16"):
        data[tvid] = [
            ("6(a)", "declarant_sale", None, None, None, None),
            ("6(b)", "spousal", None, None, None, None),
            ("6(c)", "death_probate", None, None, None, None),
            ("6(c)", "court_order", None, None, None, None),
            ("6(d)", "foreclosure", None, None, None, None),
            ("6(e)", "government_nonprofit", None, None, None, None),
            ("6(f)-(g)", "beneficiary_or_trustee", None, None, None, None),
            ("6(h)", "trustee_unidentified", None, None, None, None),
            ("6(i)", "prohibited_by_law", None, None, None, None),
            ("6(j)", "pre_effective_date", None, "recording_date", None, None),
        ]

    # ---- V09: spousal + lineal_descendant, missing trustee_unidentified
    data["V09"] = [
        ("6(a)", "declarant_sale", None, None, None, None),
        ("6(b)", "spousal", None, None, None, None),
        ("6(b)", "lineal_descendant", None, None, None, None),
        ("6(c)", "death_probate", None, None, None, None),
        ("6(c)", "court_order", None, None, None, None),
        ("6(d)", "foreclosure", None, None, None, None),
        ("6(e)", "government_nonprofit", None, None, None, None),
        ("6(f)-(g)", "beneficiary_or_trustee", None, None, None, None),
        ("6(h)", "prohibited_by_law", None, None, None, None),
        ("6(i)", "pre_effective_date", None, "recording_date", None, None),
    ]

    # ---- Data-quality flags on the seed source itself (OCR review agent's findings)
    review_notes = {
        "V17": "Source OCR collapsed whitespace across the whole document; content reconstructed and matches "
               "Family 1a exactly, but recommend a manual check against the original scan.",
        "V18": "DEFINITIONS section truncated in OCR -- Controlling Interest threshold not verifiable (left "
               "NULL). Clause (f) reads '...the Grantor owns a Controlling Interest in the Grantor...', almost "
               "certainly an OCR swap of 'the Grantee owns... in the Grantor' (every sibling template phrases "
               "it that way) -- verify against source before relying on affiliate_transaction classification "
               "for V18-governed covenants. Cutoff date OCR'd as 01/07/2012, possibly a misread of 01/01/2012.",
        "V15": "Heavier character-level OCR noise in the source template file; content reconstructed with high "
               "confidence but not individually re-verified against the scan.",
        "V16": "Heavier character-level OCR noise in the source template file; content reconstructed with high "
               "confidence but not individually re-verified against the scan.",
        "V08": "Moderate character-level OCR noise in the source template file; low ambiguity.",
    }

    rows_to_insert = []
    for tvid, rows in data.items():
        flagged = tvid in review_notes
        for clause, category, cutoff_date, cutoff_basis, ci, _ in rows:
            rows_to_insert.append({
                "template_version_id": tvid,
                "category_code": category,
                "clause_reference": clause,
                "cutoff_date": cutoff_date,
                "cutoff_basis": cutoff_basis,
                "controlling_interest_threshold_percent": ci,
                "needs_review": flagged and category in ("affiliate_transaction", "pre_effective_date"),
                "review_notes": review_notes.get(tvid) if flagged and category in
                                ("affiliate_transaction", "pre_effective_date") else None,
            })

    op.bulk_insert(table, rows_to_insert)
