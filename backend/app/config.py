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

    # --- Langfuse tracing (app/tracing.py) --------------------------------
    # Off until explicitly switched on, so local dev, the test suite and CI
    # behave exactly as they did before tracing existed. Two switches rather
    # than one on purpose: "send nothing" and "send structure but not text" are
    # different decisions, and a single flag would hide one of them.
    langfuse_enabled: bool = False
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"
    # Defaults to masking. Prompts contain users' journal entries verbatim, so
    # the safe default has to be the private one -- a deployment that wants the
    # raw text in the Langfuse UI has to say so, rather than discovering after
    # the fact that it has been shipping personal data since the day it set a key.
    langfuse_mask_inputs: bool = True
    # Tags every trace, so eval runs and real traffic can share one Langfuse
    # project without eval scores polluting the production distribution.
    langfuse_environment: str = "production"


settings = Settings()
