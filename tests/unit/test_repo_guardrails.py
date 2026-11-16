"""Static checks that enforce the AI/contributor rules from CLAUDE.md / AGENTS.md.

These tests are part of the Definition of Done - do not weaken them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MART_FILES = sorted((REPO / "db" / "mart").glob("*.sql"))
READ_ONLY_ROLES = ("grafana_reader", "bi_reader", "ai_reader")


def _strip_sql_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def test_agents_md_identical_to_claude_md():
    assert (REPO / "AGENTS.md").read_bytes() == (REPO / "CLAUDE.md").read_bytes(), (
        "AGENTS.md must be an identical copy of CLAUDE.md (cp CLAUDE.md AGENTS.md)"
    )


@pytest.mark.parametrize("name", ["add-kpi-view.md", "configure-site.md"])
def test_slash_commands_present(name):
    text = (REPO / ".claude" / "commands" / name).read_text(encoding="utf-8")
    assert text.startswith("---") and "description:" in text


# ----------------------------------------------------------------- db/mart idempotency
def test_mart_dir_not_empty_and_grants_last():
    assert MART_FILES, "db/mart/ must contain the idempotent mart SQL"
    assert MART_FILES[-1].name == "90_grants.sql"


@pytest.mark.parametrize("path", MART_FILES, ids=lambda p: p.name)
def test_mart_sql_is_idempotent_and_non_destructive(path):
    sql = _strip_sql_comments(path.read_text(encoding="utf-8")).upper()
    forbidden = [r"\bDROP\s+(TABLE|SCHEMA|DATABASE)\b", r"\bTRUNCATE\b", r"\bALTER\s+TABLE\s+CORE\.",
                 r"\bDELETE\s+FROM\s+CORE\.", r"\bUPDATE\s+CORE\."]
    for pattern in forbidden:
        assert not re.search(pattern, sql), f"{path.name}: destructive statement matches {pattern}"
    for match in re.finditer(r"\bCREATE\s+(?!OR\s+REPLACE)(?!TEMP)(\w+(?:\s+\w+)?)", sql):
        assert "IF NOT EXISTS" in sql[match.start(): match.start() + 120], (
            f"{path.name}: '{match.group(0)}' is not idempotent - use CREATE OR REPLACE / IF NOT EXISTS"
        )


# ----------------------------------------------------------------- read-only roles
SQL_SOURCES = sorted((REPO / "db").rglob("*.sql")) + sorted((REPO / "db").rglob("*.sh")) + sorted(
    (REPO / "examples").rglob("*.sql")
)


@pytest.mark.parametrize("path", SQL_SOURCES, ids=lambda p: str(p.relative_to(REPO)))
def test_read_only_roles_never_get_write_privileges(path):
    sql = _strip_sql_comments(path.read_text(encoding="utf-8"))
    for stmt in re.findall(r"GRANT\s[^;]+;", sql, flags=re.IGNORECASE | re.DOTALL):
        privileges = stmt.upper().split(" ON ")[0]
        if any(role in stmt for role in READ_ONLY_ROLES) and " TO " in stmt.upper():
            ok = re.fullmatch(r"\s*GRANT\s+(SELECT|USAGE|CONNECT)(\s*,\s*(SELECT|USAGE|CONNECT))*\s*", privileges)
            # EXECUTE only on (read-only, STABLE) functions in schema mart
            ok = ok or re.fullmatch(r"\s*GRANT\s+EXECUTE\s*", privileges) and " ON FUNCTION MART." in stmt.upper()
            assert ok, f"{path.name}: read-only role receives more than SELECT/USAGE/CONNECT: {stmt.strip()}"


def test_ai_reader_stays_read_only_with_timeout():
    sql = (REPO / "db" / "init" / "05_roles.sql").read_text(encoding="utf-8")
    assert "ALTER ROLE ai_reader SET default_transaction_read_only = on" in sql
    assert "ALTER ROLE ai_reader SET statement_timeout = '3s'" in sql


# ----------------------------------------------------------------- timestamps
def test_no_timestamp_without_time_zone():
    for path in SQL_SOURCES:
        sql = _strip_sql_comments(path.read_text(encoding="utf-8")).lower()
        assert not re.search(r"\btimestamp\b(?!tz)(?!\s+with\s+time\s+zone)", sql.replace("time_bucket", "")), (
            f"{path.name}: use timestamptz (UTC) instead of timestamp"
        )


# ----------------------------------------------------------------- compose / podman
COMPOSE = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("service", sorted(COMPOSE["services"]))
def test_compose_service_hardening(service):
    svc = COMPOSE["services"][service]
    assert "healthcheck" in svc, f"{service}: healthcheck required"
    assert svc.get("restart") == "unless-stopped"
    assert svc.get("networks") == ["oee_net"]
    assert svc.get("logging", {}).get("options", {}).get("max-file") == "5"
    for volume in svc.get("volumes", []):
        if volume.startswith((".", "/")):  # bind mount (named volumes are fine)
            flagged = volume.endswith((":z", ":ro,z", ":Z", ":ro,Z"))
            assert flagged, f"{service}: bind mount needs SELinux flag: {volume}"


def test_no_heavy_middleware():
    images = " ".join(str(s.get("image", "")) for s in COMPOSE["services"].values()).lower()
    for banned in ("kafka", "redis", "rabbitmq", "zookeeper", "airflow"):
        assert banned not in images
    assert len(COMPOSE["services"]) <= 6


# ----------------------------------------------------------------- no secrets / company data
TRACKED_TEXT = [p for p in REPO.rglob("*") if p.is_file()
                and not any(part in {".git", ".venv", "site_overrides", "__pycache__", ".pytest_cache",
                                     ".ruff_cache", "backups", "dist"} for part in p.relative_to(REPO).parts)
                and p.name not in {".env", "uv.lock", "LICENSE"}
                and p.suffix in {".py", ".sql", ".sh", ".md", ".yml", ".yaml", ".toml", ".json", ".csv", ".conf", ""}]

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"postgres(?:ql)?://[^:\s/]+:(?!\$\{|<|\.\.\.|%)[^@\s]{8,}@"),  # inline passwords in DSNs
    re.compile(r"(?i)password(?:\s*=|:)\s*['\"][^'\"$<{]{8,}['\"]"),  # literal, not psql :'var'
]


@pytest.mark.parametrize("path", TRACKED_TEXT, ids=lambda p: str(p.relative_to(REPO)))
def test_no_secrets_in_tracked_files(path):
    text = path.read_text(encoding="utf-8", errors="ignore")
    for pattern in SECRET_PATTERNS:
        assert not pattern.search(text), f"possible secret in {path.relative_to(REPO)}: {pattern.pattern}"


def test_python_services_have_no_domain_aggregations():
    """SQL = domain logic: services must not compute percentiles/medians/OEE themselves."""
    banned = re.compile(r"\b(statistics\.(median|quantiles|mean)|numpy|pandas|np\.percentile|quantile\()")
    for path in (REPO / "services").rglob("*.py"):
        assert not banned.search(path.read_text(encoding="utf-8")), f"domain calculation in Python: {path}"
