"""Settings validation: a mistake in .env should fail at startup with a message that says what to do."""
import os

os.environ.setdefault("GOOGLE_API_KEY", "test-key-not-used")

import pytest
from pydantic import ValidationError

from src.config.settings import Settings


def make(**values) -> Settings:
    return Settings(_env_file=None, **values)


def test_groq_is_the_default_provider_with_a_real_llama_model():
    settings = make()
    assert settings.llm_provider == "groq" and settings.groq_model.startswith("llama")


@pytest.mark.parametrize("name, expected", [
    ("gemini-3.5-flash", "gemini-3.5-flash"),
    ("gemini-2.5-flash-lite", "gemini-2.5-flash-lite"),
    ("models/gemini-3.5-flash", "gemini-3.5-flash"),          # the API's own prefix is tolerated and stripped
    ("  gemini-3.5-flash  ", "gemini-3.5-flash"),
])
def test_valid_gemini_model_ids_are_accepted(name, expected):
    assert make(llm_provider="gemini", gemini_model=name).gemini_model == expected


@pytest.mark.parametrize("name", ["Gemini 3.1 Flash-Lite", "Gemini-3.5-Flash", "gemini 3.5 flash", "", "-flash"])
def test_a_gemini_display_name_is_rejected_with_a_helpful_message(name):
    with pytest.raises(ValidationError) as error:
        make(llm_provider="gemini", gemini_model=name)
    assert "not a model ID" in str(error.value) and "gemini-3.5-flash" in str(error.value)


@pytest.mark.parametrize("name", ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "meta-llama/llama-4-scout-17b-16e-instruct"])
def test_valid_groq_model_ids_are_accepted(name):
    assert make(llm_provider="groq", groq_model=name).groq_model == name


@pytest.mark.parametrize("name", ["Llama 3.3 70B", "Llama-3.3-70b", "llama 3", ""])
def test_a_groq_display_name_is_rejected_with_a_helpful_message(name):
    with pytest.raises(ValidationError) as error:
        make(llm_provider="groq", groq_model=name)
    assert "not a model ID" in str(error.value) and "llama-3.3-70b-versatile" in str(error.value)


def test_a_stale_value_for_the_other_provider_does_not_block_startup():
    """A bad GEMINI_MODEL in .env must not stop the app that runs on Groq (and vice versa)."""
    assert make(llm_provider="groq", gemini_model="Gemini 3.1 Flash-Lite").llm_provider == "groq"
    assert make(llm_provider="gemini", groq_model="Llama 3.3 70B").llm_provider == "gemini"


def test_an_unknown_provider_is_rejected():
    with pytest.raises(ValidationError):
        make(llm_provider="skynet")


def test_the_llm_retry_defaults_are_modest():
    settings = make()
    assert (settings.llm_max_retries, settings.llm_attempts, settings.llm_timeout_seconds) == (2, 2, 60.0)
