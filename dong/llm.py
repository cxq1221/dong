"""LLM API wrapper — OpenAI-compatible, auto-loads .env from project root."""
import os, pathlib
from openai import OpenAI

# Auto-load .env from project root (no dependency needed)
_env_path = pathlib.Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

_client = None

def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=os.getenv("DONG_API_KEY") or os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("DONG_BASE_URL", "https://api.openai.com/v1"),
        )
    return _client


def get_model():
    return os.getenv("DONG_MODEL", "gpt-4o")


def chat(messages, tools, model=None):
    """One LLM call with function calling. Returns the response message."""
    return _get_client().chat.completions.create(
        model=model or get_model(),
        messages=messages,
        tools=tools,
        temperature=0,
    ).choices[0].message
