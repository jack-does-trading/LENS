from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://lens:lens@localhost:5432/lens"
    ingestion_api_key: str | None = None
    voyage_api_key: str | None = None
    voyage_model: str = "voyage-3"
    embedding_dimension: int = 1024
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.2:3b"
    # "ollama" (default, fully local) or "groq" (hosted, see app/llm.py's
    # GroqLLMClient -- deliberately opt-in, reverses OllamaLLMClient's
    # localhost-only privacy guarantee when chosen).
    llm_provider: str = "ollama"
    groq_api_key: str | None = None
    # A bigger model than tools/local_extraction defaults to on purpose --
    # this pipeline needs the model to reliably self-correct against
    # verification.issues on retry (strict second-person voice, exact
    # quote limits), which is exactly where small models are weakest.
    # Was "llama-3.3-70b-versatile" -- Groq decommissioned that model (and
    # llama-3.1-8b-instant) on 2026-08-16; openai/gpt-oss-120b is Groq's
    # recommended same-class replacement. See docs/DEPLOYMENT.md for the
    # Render dashboard env var this default doesn't reach on its own.
    groq_model: str = "openai/gpt-oss-120b"
    cors_allow_origins: list[str] = ["http://localhost:3000"]


settings = Settings()
