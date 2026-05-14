"""
pytest conftest.py — shared fixtures and test environment setup.
"""
import os
import pytest

# Set test env vars before any imports
os.environ.setdefault("ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY", "test-key"))
os.environ.setdefault("VAULT_MASTER_PASSWORD", "test-vault-password-for-pytest")
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost:5432/test_db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")  # DB 1 for tests


@pytest.fixture(scope="session")
def event_loop():
    """Use single event loop for all async tests in a session."""
    import asyncio
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def clean_seen_hashes():
    """Reset scraper dedup set between tests."""
    from agents.job_discovery import scraper
    scraper._seen.clear()   # renamed to _seen in the rewrite
    yield
    scraper._seen.clear()
