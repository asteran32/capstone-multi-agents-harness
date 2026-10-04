import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv('DATABASE_URL', 'postgresql+psycopg://guardrail:guardrail@localhost:5432/guardrail'))
    username: str = field(default_factory=lambda: os.getenv('APP_USERNAME', 'researcher'))
    password: str = field(default_factory=lambda: os.getenv('APP_PASSWORD', ''))
    openai_key: str = field(default_factory=lambda: os.getenv('OPENAI_API_KEY', ''))
    live_models: tuple[str, ...] = field(default_factory=lambda: tuple(m.strip() for m in os.getenv('OPENAI_MODELS', '').split(',') if m.strip()))
    sandbox_url: str = field(default_factory=lambda: os.getenv('SANDBOX_URL', 'http://sandbox:8090'))
    sandbox_token: str = field(default_factory=lambda: os.getenv('SANDBOX_TOKEN', ''))
    sandbox_mode: str = field(default_factory=lambda: os.getenv('SANDBOX_MODE', 'docker'))
    frontend: Path = field(default_factory=lambda: Path(os.getenv('FRONTEND_DIST', str(ROOT / 'frontend/dist'))))


settings = Settings()
