"""The LLM factory: one switch (LLM_PROVIDER) picks Groq (Llama) or Gemini, and a missing key fails
fast with the fix — no network calls."""
import os

os.environ.setdefault("GOOGLE_API_KEY", "test-key-not-used")

from types import SimpleNamespace

import pytest

from src.config import model_factory
from src.config.model_factory import LlmConfigurationError, LlmModelFactory, check_llm_configuration


def settings(**overrides):
    base = dict(
        llm_provider="groq", groq_api_key="gk", groq_model="llama-3.3-70b-versatile",
        google_api_key="goog", gemini_model="gemini-2.5-flash", llm_max_retries=2, llm_timeout_seconds=60.0,
    )
    return SimpleNamespace(**{**base, **overrides})


@pytest.fixture
def captured(monkeypatch):
    """Replace both chat-model classes with recorders, so nothing is constructed for real."""
    built = []

    def recorder(name):
        return lambda **kwargs: built.append((name, kwargs)) or SimpleNamespace(provider=name)

    monkeypatch.setattr(model_factory, "ChatGroq", recorder("groq"))
    monkeypatch.setattr(model_factory, "ChatGoogleGenerativeAI", recorder("gemini"))
    return built


def use(monkeypatch, **overrides):
    monkeypatch.setattr(model_factory, "get_settings", lambda: settings(**overrides))


def test_groq_builds_llama_with_the_configured_limits(monkeypatch, captured):
    use(monkeypatch)
    model = LlmModelFactory().get_model(temperature=0.0)
    assert model.provider == "groq"
    assert captured == [("groq", {"model": "llama-3.3-70b-versatile", "temperature": 0.0, "api_key": "gk",
                                  "max_retries": 2, "timeout": 60.0})]


def test_gemini_is_still_available_by_switching_the_provider(monkeypatch, captured):
    use(monkeypatch, llm_provider="gemini")
    assert LlmModelFactory().get_model().provider == "gemini"
    name, kwargs = captured[0]
    assert (name, kwargs["model"], kwargs["google_api_key"], kwargs["max_retries"]) == ("gemini", "gemini-2.5-flash", "goog", 2)


def test_a_missing_groq_key_fails_with_the_exact_fix(monkeypatch, captured):
    use(monkeypatch, groq_api_key="")
    with pytest.raises(LlmConfigurationError) as error:
        LlmModelFactory().get_model()
    assert "GROQ_API_KEY" in str(error.value) and "console.groq.com" in str(error.value) and captured == []


def test_a_missing_gemini_key_fails_when_gemini_is_selected(monkeypatch):
    use(monkeypatch, llm_provider="gemini", google_api_key="")
    with pytest.raises(LlmConfigurationError, match="GOOGLE_API_KEY"):
        check_llm_configuration()


def test_the_other_providers_missing_key_is_not_a_problem(monkeypatch):
    use(monkeypatch, llm_provider="groq", google_api_key="")        # Gemini unused
    assert check_llm_configuration() == "groq / llama-3.3-70b-versatile"
    use(monkeypatch, llm_provider="gemini", groq_api_key="")        # Groq unused
    assert check_llm_configuration() == "gemini / gemini-2.5-flash"


def test_the_shared_instance_is_built_once(monkeypatch, captured):
    use(monkeypatch)
    model_factory.get_deal_parser_llm.cache_clear()
    assert model_factory.get_deal_parser_llm() is model_factory.get_deal_parser_llm()
    assert len(captured) == 1 and captured[0][1]["temperature"] == 0.0
    model_factory.get_deal_parser_llm.cache_clear()
