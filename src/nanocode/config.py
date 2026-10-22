from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class Settings:
    workspace: Path
    data_dir: Path
    base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"
    model: str = "qwen3.5:9b"
    max_steps: int = 16
    ai_review: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        workspace = Path(os.environ.get("NANO_WORKSPACE", os.getcwd())).resolve()
        data_dir = Path(os.environ.get("NANO_DATA_DIR", workspace / ".nano-code")).resolve()
        return cls(
            workspace=workspace,
            data_dir=data_dir,
            base_url=os.environ.get("NANO_BASE_URL", "http://localhost:11434/v1"),
            api_key=os.environ.get("NANO_API_KEY", "ollama"),
            model=os.environ.get("NANO_MODEL", "qwen3.5:9b"),
            ai_review=os.environ.get("NANO_AI_REVIEW", "").lower() in {"1", "true", "yes"},
        )
