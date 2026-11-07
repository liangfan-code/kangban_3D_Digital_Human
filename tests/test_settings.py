"""Settings 加载测试：.env / 环境变量；后续变量为空不影响 Day1。"""

from app.core.config import Settings, get_settings


def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test-env")
    monkeypatch.setenv("APP_PORT", "9999")
    monkeypatch.setattr("app.core.config.get_settings.cache_clear", lambda: None)
    settings = Settings()
    assert settings.app_env == "test-env"
    assert settings.app_port == 9999


def test_settings_defaults():
    settings = Settings()
    assert settings.app_env == "local"
    assert settings.app_timezone == "Asia/Shanghai"
    assert settings.audio_retention_hours == 72
    assert settings.database_url.startswith("postgresql+asyncpg://")


def test_future_optional_fields_empty_by_default():
    """后续能力变量为空/占位时，Settings 可正常构造；JWT 配置自 Day2 起启用。"""
    settings = Settings()
    assert settings.llm_api_key in (None, "")
    # Day2：JWT 默认值生效；测试环境注入了测试密钥
    assert settings.jwt_access_token_expire_minutes == 60
    assert settings.device_token_expire_days == 90
    assert settings.device_registration_token_hours == 24


def test_get_settings_cached():
    assert get_settings() is get_settings()
