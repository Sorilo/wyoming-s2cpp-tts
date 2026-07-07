from app.config import Settings


def test_default_model_targets_q6():
    settings = Settings()
    assert settings.s2_model == "/models/s2-pro-q6_k.gguf"


def test_default_wyoming_uri():
    settings = Settings()
    assert settings.wyoming_uri == "tcp://0.0.0.0:10200"


def test_from_env_overrides_s2_host_and_port(monkeypatch):
    monkeypatch.setenv("S2_HOST", "192.168.1.45")
    monkeypatch.setenv("S2_PORT", "3131")

    settings = Settings.from_env()

    assert settings.s2_host == "192.168.1.45"
    assert settings.s2_port == 3131


def test_from_env_keeps_defaults_when_s2_env_missing(monkeypatch):
    monkeypatch.delenv("S2_HOST", raising=False)
    monkeypatch.delenv("S2_PORT", raising=False)

    settings = Settings.from_env()

    assert settings.s2_host == "127.0.0.1"
    assert settings.s2_port == 3030
