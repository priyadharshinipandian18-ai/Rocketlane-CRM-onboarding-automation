"""Loads prompt text from this folder, so no prompt lives inside Python code.

    load_prompt("communication/agent_system")   →   the contents of  src/prompts/communication/agent_system.md

Files are plain text (they are sent to the model as they are, so keep notes and comments out of them). A prompt
that is a template uses ``{name}`` placeholders: LangChain prompt templates fill their own, and ``render_prompt``
fills the ones in prompts that are not templates. Prompts are read once and cached; a missing file fails loudly
with the path it looked for.
"""
import functools
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent


class PromptNotFoundError(FileNotFoundError):
    """The named prompt has no file in ``src/prompts``."""


@functools.cache
def load_prompt(name: str) -> str:
    """The text of ``src/prompts/<name>.md`` (trailing newlines removed)."""
    path = (PROMPTS_DIR / f"{name}.md").resolve()
    if PROMPTS_DIR not in path.parents:
        raise ValueError(f"Prompt name {name!r} points outside the prompts folder")
    try:
        return path.read_text(encoding="utf-8").rstrip("\n")
    except FileNotFoundError:
        raise PromptNotFoundError(f"No prompt file for {name!r}: expected {path}") from None


def render_prompt(name: str, **values: object) -> str:
    """``load_prompt`` with each ``{key}`` replaced by its value. Plain replacement (not ``str.format``), so any
    other braces in the prompt are left alone."""
    text = load_prompt(name)
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text
