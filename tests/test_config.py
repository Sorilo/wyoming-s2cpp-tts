from app.config import Settings


def test_default_model_targets_q6():
    settings = Settings()
    assert settings.s2_model == "/models/s2-pro-q6_k.gguf"


def test_default_wyoming_uri():
    settings = Settings()
    assert settings.wyoming_uri == "tcp://0.0.0.0:10200"
