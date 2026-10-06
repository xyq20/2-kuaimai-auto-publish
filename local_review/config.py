from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PRODUCTS_ROOT = Path.home() / "Desktop" / "共享文件" / "谭" / "products"


def default_products_root() -> Path:
    candidates = (
        Path("/Volumes/共享文件") / "谭" / "products",
        DEFAULT_PRODUCTS_ROOT,
    )
    return next((path for path in candidates if path.is_dir()), candidates[0])


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    device_token: str
    model_api_url: str = ""
    model_api_key: str = ""
    ai_model: str = "gpt-5.1"
    products_root: Path = default_products_root()
    review_port: int = 8787

    @property
    def database_path(self) -> Path:
        return self.data_dir / "review.sqlite3"

    @property
    def assets_dir(self) -> Path:
        return self.data_dir / "assets"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(
                os.environ.get(
                    "KUAIMAI_REVIEW_DATA_DIR",
                    str(PROJECT_DIR / ".local-state" / "review-center"),
                )
            ).expanduser(),
            device_token=os.environ.get(
                "KUAIMAI_REVIEW_DEVICE_TOKEN",
                os.environ.get("KUAIMAI_LEARNING_DEVICE_TOKEN", ""),
            ),
            model_api_url=os.environ.get("KUAIMAI_MODEL_API_URL", ""),
            model_api_key=os.environ.get("KUAIMAI_MODEL_API_KEY", ""),
            ai_model=os.environ.get("KUAIMAI_AI_MODEL", "gpt-5.1"),
            products_root=Path(
                os.environ.get("KUAIMAI_PRODUCTS_ROOT", str(default_products_root()))
            ).expanduser(),
            review_port=int(os.environ.get("KUAIMAI_REVIEW_PORT", "8787")),
        )
