from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path

from product_materials import default_materials_root


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
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-flash"
    materials_root: Path | None = None

    @property
    def database_path(self) -> Path:
        return self.data_dir / "review.sqlite3"

    @property
    def assets_dir(self) -> Path:
        return self.data_dir / "assets"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    def with_deepseek_configuration(self) -> "Settings":
        """Reload only text-model settings; keep database and device identity."""
        try:
            config = json.loads((self.data_dir / "deepseek.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = {}
        if not isinstance(config, dict):
            config = {}
        return replace(self,
            deepseek_api_key=str(os.environ.get("KUAIMAI_DEEPSEEK_API_KEY", os.environ.get("DEEPSEEK_API_KEY", config.get("api_key", self.deepseek_api_key)))).strip(),
            deepseek_model=str(os.environ.get("KUAIMAI_DEEPSEEK_MODEL", config.get("model", self.deepseek_model))).strip(),
        )

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.environ.get("KUAIMAI_REVIEW_DATA_DIR", str(PROJECT_DIR / ".local-state" / "review-center"))).expanduser()
        try:
            deepseek = json.loads((data_dir / "deepseek.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            deepseek = {}
        if not isinstance(deepseek, dict):
            deepseek = {}
        return cls(
            data_dir=data_dir,
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
            materials_root=default_materials_root(),
            review_port=int(os.environ.get("KUAIMAI_REVIEW_PORT", "8787")),
            deepseek_api_key=str(os.environ.get("KUAIMAI_DEEPSEEK_API_KEY", os.environ.get("DEEPSEEK_API_KEY", deepseek.get("api_key", "")))).strip(),
            deepseek_model=str(os.environ.get("KUAIMAI_DEEPSEEK_MODEL", deepseek.get("model", "deepseek-flash"))).strip(),
        )
