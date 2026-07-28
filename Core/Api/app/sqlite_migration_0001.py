"""Frozen SQLite schema for migration 1.

Do not derive this revision from SQLAlchemy metadata or edit it when models
change. Future schema changes must be added as a new numbered migration.
"""

SQLITE_MIGRATION_0001 = (
    """
    CREATE TABLE IF NOT EXISTS deployments (
        id INTEGER NOT NULL PRIMARY KEY,
        computer_name VARCHAR(63) NOT NULL,
        serial_number VARCHAR(128),
        model VARCHAR(128),
        manufacturer VARCHAR(128),
        system_sku VARCHAR(128),
        mac_address VARCHAR(17) NOT NULL,
        ip_address VARCHAR(45) NOT NULL,
        image_name VARCHAR(255),
        domain_join BOOLEAN NOT NULL,
        last_error_message TEXT,
        status VARCHAR(16) NOT NULL,
        started_at DATETIME NOT NULL,
        completed_at DATETIME,
        CONSTRAINT ck_deployments_status
            CHECK (status IN ('begin', 'completed', 'failed')),
        CONSTRAINT ck_deployments_completion CHECK (
            (status = 'begin' AND completed_at IS NULL) OR
            (status IN ('completed', 'failed') AND completed_at IS NOT NULL)
        )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS deployment_stages (
        id INTEGER NOT NULL PRIMARY KEY,
        deployment_id INTEGER NOT NULL,
        stage VARCHAR(64) NOT NULL,
        phase VARCHAR(16) NOT NULL,
        status VARCHAR(16) NOT NULL,
        started_at DATETIME NOT NULL,
        completed_at DATETIME,
        error_message TEXT,
        CONSTRAINT ck_deployment_stages_phase CHECK (phase IN ('winpe')),
        CONSTRAINT ck_deployment_stages_status
            CHECK (status IN ('running', 'completed', 'failed', 'skipped')),
        CONSTRAINT ck_deployment_stages_completion CHECK (
            (status = 'running' AND completed_at IS NULL) OR
            (status IN ('completed', 'failed', 'skipped')
             AND completed_at IS NOT NULL)
        ),
        CONSTRAINT uq_deployment_stages_deployment_stage
            UNIQUE (deployment_id, stage),
        FOREIGN KEY(deployment_id) REFERENCES deployments (id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS deployment_network_summaries (
        deployment_id INTEGER NOT NULL PRIMARY KEY,
        started_at DATETIME NOT NULL,
        completed_at DATETIME NOT NULL,
        ping_target VARCHAR(255) NOT NULL,
        smb_adapter_name VARCHAR(255),
        smb_adapter_description VARCHAR(512),
        smb_adapter_id VARCHAR(255),
        smb_local_ip VARCHAR(45),
        smb_link_speed_bps BIGINT,
        api_adapter_name VARCHAR(255),
        api_adapter_description VARCHAR(512),
        api_adapter_id VARCHAR(255),
        api_local_ip VARCHAR(45),
        api_link_speed_bps BIGINT,
        adapters_differ BOOLEAN NOT NULL,
        duration_seconds FLOAT NOT NULL,
        icmp_status VARCHAR(16) NOT NULL,
        ping_sent INTEGER NOT NULL,
        ping_received INTEGER NOT NULL,
        ping_lost INTEGER NOT NULL,
        loss_percentage FLOAT,
        rtt_min_ms FLOAT,
        rtt_avg_ms FLOAT,
        rtt_max_ms FLOAT,
        latency_spikes INTEGER NOT NULL,
        bytes_received BIGINT,
        average_inbound_mbps FLOAT,
        link_utilization_percent FLOAT,
        api_request_count INTEGER NOT NULL,
        api_error_count INTEGER NOT NULL,
        api_min_ms FLOAT,
        api_avg_ms FLOAT,
        api_max_ms FLOAT,
        smb_connect_success BOOLEAN,
        smb_connect_attempts INTEGER NOT NULL,
        smb_connect_duration_ms FLOAT,
        smb_error_message TEXT,
        diagnostic_errors JSON NOT NULL,
        CONSTRAINT ck_deployment_network_summaries_icmp_status
            CHECK (icmp_status IN ('available', 'unavailable', 'not_measured')),
        FOREIGN KEY(deployment_id) REFERENCES deployments (id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS deployment_network_stages (
        id INTEGER NOT NULL PRIMARY KEY,
        deployment_id INTEGER NOT NULL,
        stage VARCHAR(64) NOT NULL,
        started_at DATETIME NOT NULL,
        completed_at DATETIME NOT NULL,
        duration_seconds FLOAT NOT NULL,
        icmp_status VARCHAR(16) NOT NULL,
        ping_sent INTEGER NOT NULL,
        ping_received INTEGER NOT NULL,
        ping_lost INTEGER NOT NULL,
        loss_percentage FLOAT,
        rtt_min_ms FLOAT,
        rtt_avg_ms FLOAT,
        rtt_max_ms FLOAT,
        latency_spikes INTEGER NOT NULL,
        bytes_received BIGINT,
        average_inbound_mbps FLOAT,
        link_utilization_percent FLOAT,
        CONSTRAINT ck_deployment_network_stages_stage
            CHECK (stage IN (
                'image_apply', 'driver_injection', 'postinstall_copy'
            )),
        CONSTRAINT ck_deployment_network_stages_icmp_status
            CHECK (icmp_status IN ('available', 'unavailable', 'not_measured')),
        CONSTRAINT uq_deployment_network_stages_deployment_stage
            UNIQUE (deployment_id, stage),
        FOREIGN KEY(deployment_id) REFERENCES deployments (id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS deployment_programs (
        id INTEGER NOT NULL PRIMARY KEY,
        deployment_id INTEGER NOT NULL,
        position INTEGER NOT NULL,
        name VARCHAR(255) NOT NULL,
        status VARCHAR(16) NOT NULL,
        exit_code INTEGER,
        duration_seconds INTEGER NOT NULL,
        reason VARCHAR(32),
        error_message TEXT,
        CONSTRAINT ck_deployment_programs_status
            CHECK (status IN ('installed', 'failed', 'timed_out')),
        CONSTRAINT uq_deployment_programs_deployment_position
            UNIQUE (deployment_id, position),
        FOREIGN KEY(deployment_id) REFERENCES deployments (id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS computers (
        id INTEGER NOT NULL PRIMARY KEY,
        serial_number VARCHAR(128),
        mac_address VARCHAR(17),
        last_model VARCHAR(128),
        last_computer_name VARCHAR(63) NOT NULL,
        last_ip_address VARCHAR(45),
        last_image_name VARCHAR(255),
        last_domain_join BOOLEAN NOT NULL,
        deployment_count INTEGER NOT NULL,
        first_seen_at DATETIME NOT NULL,
        last_seen_at DATETIME NOT NULL,
        last_deployment_id INTEGER,
        FOREIGN KEY(last_deployment_id) REFERENCES deployments (id)
            ON DELETE SET NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS auth_users (
        id INTEGER NOT NULL PRIMARY KEY,
        username VARCHAR(64) NOT NULL,
        username_normalized VARCHAR(64) NOT NULL,
        password_hash VARCHAR(255) NOT NULL,
        is_active BOOLEAN NOT NULL,
        is_superadmin BOOLEAN NOT NULL,
        failed_login_count INTEGER NOT NULL,
        locked_until DATETIME,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS auth_permissions (
        user_id INTEGER NOT NULL,
        permission VARCHAR(32) NOT NULL,
        PRIMARY KEY (user_id, permission),
        FOREIGN KEY(user_id) REFERENCES auth_users (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS auth_sessions (
        id INTEGER NOT NULL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        token_hash VARCHAR(64) NOT NULL,
        created_at DATETIME NOT NULL,
        last_seen_at DATETIME NOT NULL,
        expires_at DATETIME NOT NULL,
        FOREIGN KEY(user_id) REFERENCES auth_users (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS deployment_tokens (
        id INTEGER NOT NULL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        deployment_id INTEGER,
        token_hash VARCHAR(64) NOT NULL,
        phase VARCHAR(16) NOT NULL,
        created_at DATETIME NOT NULL,
        last_seen_at DATETIME NOT NULL,
        expires_at DATETIME NOT NULL,
        revoked_at DATETIME,
        FOREIGN KEY(user_id) REFERENCES auth_users (id) ON DELETE CASCADE,
        FOREIGN KEY(deployment_id) REFERENCES deployments (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS winpe_auth_policy (
        id INTEGER NOT NULL PRIMARY KEY,
        mode VARCHAR(16) NOT NULL,
        pin_hash VARCHAR(255),
        updated_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS winpe_pin_attempts (
        client_key VARCHAR(64) NOT NULL PRIMARY KEY,
        failed_count INTEGER NOT NULL,
        locked_until DATETIME,
        updated_at DATETIME NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_deployments_status ON deployments (status)",
    """
    CREATE INDEX IF NOT EXISTS ix_deployments_computer_name
        ON deployments (computer_name)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_deployment_stages_deployment_id
        ON deployment_stages (deployment_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_deployment_network_stages_deployment_id
        ON deployment_network_stages (deployment_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_deployment_programs_deployment_id
        ON deployment_programs (deployment_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_computers_serial_number
        ON computers (serial_number)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_computers_mac_address
        ON computers (mac_address)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_computers_last_computer_name
        ON computers (last_computer_name)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ix_auth_users_username_normalized
        ON auth_users (username_normalized)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ix_auth_sessions_token_hash
        ON auth_sessions (token_hash)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_auth_sessions_user_id
        ON auth_sessions (user_id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ix_deployment_tokens_token_hash
        ON deployment_tokens (token_hash)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_deployment_tokens_user_id
        ON deployment_tokens (user_id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ix_deployment_tokens_deployment_id
        ON deployment_tokens (deployment_id)
    """,
)
