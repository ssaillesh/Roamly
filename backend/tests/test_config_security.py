"""Production refuses to start without a real JWT signing secret."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import config
from app.config import DEV_JWT_SECRET, ConfigError, Settings, check_production

BACKEND = Path(__file__).resolve().parents[1]
GOOD = "c0ffee" * 11
SITE = "https://app.example.com"


def _settings(**kw) -> Settings:
    # A valid production site, so these tests are about the secret alone.
    return Settings(_env_file=None, **{"frontend_base_url": SITE, **kw})


@pytest.mark.parametrize("secret", ["", "   ", "\n", DEV_JWT_SECRET, f" {DEV_JWT_SECRET}\n"])
def test_production_refuses_a_missing_empty_or_default_secret(secret):
    with pytest.raises(ConfigError) as err:
        check_production(_settings(environment="production", jwt_secret=secret))
    assert "JWT_SECRET" in str(err.value)
    assert DEV_JWT_SECRET not in str(err.value)                 # never echoes the value


def test_production_with_a_real_secret_passes():
    check_production(_settings(environment="production", jwt_secret=GOOD))


@pytest.mark.parametrize("env", ["Production", " production ", "PRODUCTION"])
def test_production_is_recognised_however_it_is_typed(env):
    s = _settings(environment=env)
    assert s.environment == "production"
    with pytest.raises(ConfigError):
        check_production(s)


@pytest.mark.parametrize("env", ["development", "test", "staging"])
def test_other_environments_keep_the_development_default(env):
    s = _settings(environment=env)
    assert s.jwt_secret == DEV_JWT_SECRET
    check_production(s)


def test_get_settings_runs_the_check_when_the_variable_is_absent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)                                 # no .env file to fall back on
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("FRONTEND_BASE_URL", SITE)
    with pytest.raises(ConfigError, match="JWT_SECRET"):
        config.get_settings.__wrapped__()
    monkeypatch.setenv("JWT_SECRET", GOOD)
    assert config.get_settings.__wrapped__().jwt_secret == GOOD


def _start(env_overrides: dict) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "JWT_SECRET"}
    env.update(env_overrides)
    return subprocess.run([sys.executable, "-B", "-c", "import app.main"], cwd=BACKEND, env=env,
                          capture_output=True, text=True, timeout=120)


def test_the_app_will_not_start_in_production_with_the_default():
    # backend/.env may hold a dev secret; an explicit env var wins over it.
    bad = _start({"ENVIRONMENT": "production", "JWT_SECRET": DEV_JWT_SECRET})
    assert bad.returncode != 0
    assert "ConfigError" in bad.stderr and "JWT_SECRET is missing" in bad.stderr
    assert DEV_JWT_SECRET not in bad.stderr + bad.stdout
    good = _start({"ENVIRONMENT": "production", "JWT_SECRET": GOOD, "FRONTEND_BASE_URL": SITE})
    assert good.returncode == 0, good.stderr[-2000:]
    assert GOOD not in good.stderr + good.stdout


def test_tokens_are_signed_with_the_checked_settings():
    from app.services import security
    assert security.settings is config.settings
