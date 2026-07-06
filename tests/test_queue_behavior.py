from app.config import Settings


def test_initial_queue_policy_is_bounded_single_worker_friendly():
    settings = Settings()
    assert settings.max_queue_size == 3
    assert settings.cancel_on_new_request is False
    assert settings.cancel_on_client_disconnect is True
