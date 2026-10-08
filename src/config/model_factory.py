"""Builds the chat model behind the email classifier and the deal parser.

``LLM_PROVIDER`` picks the host — ``groq`` (hosted Llama, the default) or ``gemini`` — and the rest of
the app only ever sees a LangChain chat model, so nothing else changes when you switch. Both are
API-key based; no cloud project is needed.
"""
import functools

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq

from src.config.settings import Settings, get_settings


class LlmConfigurationError(RuntimeError):
    """The selected LLM provider isn't usable as configured (e.g. its API key is missing)."""


def check_llm_configuration(settings: Settings | None = None) -> str:
    """Fail fast, with the exact fix, if the chosen provider can't work. Returns a description of it."""
    settings = settings or get_settings()
    if settings.llm_provider == "groq":
        if not settings.groq_api_key:
            raise LlmConfigurationError(
                "LLM_PROVIDER=groq but GROQ_API_KEY is not set. Create a key at https://console.groq.com/keys and "
                "put it in .env (or set LLM_PROVIDER=gemini)."
            )
        return f"groq / {settings.groq_model}"
    if not settings.google_api_key:
        raise LlmConfigurationError(
            "LLM_PROVIDER=gemini but GOOGLE_API_KEY is not set (or set LLM_PROVIDER=groq with GROQ_API_KEY)."
        )
    return f"gemini / {settings.gemini_model}"


class LlmModelFactory:
    """Builds chat models for the configured provider."""

    def get_model(self, temperature: float = 0.1) -> BaseChatModel:
        settings = get_settings()
        check_llm_configuration(settings)
        if settings.llm_provider == "groq":
            return ChatGroq(
                model=settings.groq_model,
                temperature=temperature,
                api_key=settings.groq_api_key,
                max_retries=settings.llm_max_retries,
                timeout=settings.llm_timeout_seconds,
            )
        return ChatGoogleGenerativeAI(
            model=settings.gemini_model,
            temperature=temperature,
            google_api_key=settings.google_api_key,
            max_retries=settings.llm_max_retries,
            timeout=settings.llm_timeout_seconds,
        )


@functools.cache
def get_deal_parser_llm() -> BaseChatModel:
    """Cached singleton used by the email-classifier and deal-parser nodes."""
    return LlmModelFactory().get_model(temperature=0.0)


@functools.cache
def get_agent_llm() -> BaseChatModel:
    """Cached singleton behind the Slack communication agent: a little warmer than the classifiers, because
    it writes the customer's welcome message (tool choice is still constrained by the tool schemas)."""
    return LlmModelFactory().get_model(temperature=0.3)
