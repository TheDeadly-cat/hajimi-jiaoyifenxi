from __future__ import annotations

import os
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIST = PROJECT_DIR / "frontend" / "dist"


def _load_local_env() -> None:
    if os.getenv("AI_STUDIO_SKIP_LOCAL_ENV", "").strip().lower() in {
        "1", "true", "yes",
    }:
        return
    candidates = [PROJECT_DIR / ".env.local"]
    candidates.extend(parent / ".env.local" for parent in PROJECT_DIR.parents[:4])
    for path in candidates:
        if not path.is_file():
            continue
        try:
            for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                clean_name = name.strip()
                if clean_name not in {
                    "OPENAI_API_KEY",
                    "OPENAI_BASE_URL",
                    "OPENAI_MODEL",
                    "DEEPSEEK_API_KEY",
                    "DEEPSEEK_BASE_URL",
                    "DEEPSEEK_MODEL",
                    "ARK_API_KEY",
                    "ARK_BASE_URL",
                    "ARK_MODEL",
                    "GLM_API_KEY",
                    "ZHIPUAI_API_KEY",
                    "GLM_BASE_URL",
                    "GLM_MODEL",
                    "AI_STUDIO_DEFAULT_PROVIDER",
                    "AI_STUDIO_DISABLED_PROVIDERS",
                    "FUTU_HOST",
                    "FUTU_PORT",
                    "FUTU_CACHE_TTL_SECONDS",
                    "SEC_USER_AGENT",
                    "SEC_CACHE_TTL_SECONDS",
                }:
                    continue
                clean_value = value.strip().strip('"').strip("'")
                if clean_value:
                    os.environ.setdefault(clean_name, clean_value)
            return
        except OSError:
            continue


_load_local_env()


def _configured_path(name: str, default: Path) -> Path:
    raw_value = os.getenv(name, "").strip()
    return Path(raw_value).expanduser().resolve() if raw_value else default.resolve()


def _configured_provider_ids(name: str, default: str) -> frozenset[str]:
    raw_value = os.getenv(name, default)
    return frozenset(
        provider_id
        for provider_id in (
            item.strip().lower() for item in raw_value.split(",")
        )
        if provider_id
    )


HARD_DISABLED_PROVIDER_IDS = frozenset({"openai"})


def _deployment_disabled_provider_ids(
    name: str,
    default: str,
) -> frozenset[str]:
    """Return deployment disables plus providers that policy cannot re-enable."""

    return _configured_provider_ids(name, default) | HARD_DISABLED_PROVIDER_IDS


RUNTIME_DIR = _configured_path("AI_STUDIO_RUNTIME_DIR", PROJECT_DIR / "runtime")
DATABASE_PATH = _configured_path(
    "AI_STUDIO_DATABASE_PATH",
    RUNTIME_DIR / "collaboration_studio.sqlite3",
)
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
ARK_API_KEY = os.getenv("ARK_API_KEY", "")
ARK_BASE_URL = os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
ARK_MODEL = os.getenv("ARK_MODEL", "doubao-seed-2-0-lite-260215")
GLM_API_KEY = os.getenv("GLM_API_KEY", "") or os.getenv("ZHIPUAI_API_KEY", "")
GLM_BASE_URL = os.getenv("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
GLM_MODEL = os.getenv("GLM_MODEL", "glm-5.2")
DEFAULT_PROVIDER = os.getenv("AI_STUDIO_DEFAULT_PROVIDER", "deepseek").strip().lower() or "deepseek"
DISABLED_PROVIDER_IDS = _deployment_disabled_provider_ids(
    "AI_STUDIO_DISABLED_PROVIDERS",
    "",
)
HOST = os.getenv("AI_STUDIO_HOST", "127.0.0.1")
PORT = int(os.getenv("AI_STUDIO_PORT", "8770"))
FUTU_HOST = os.getenv("FUTU_HOST", "127.0.0.1")
FUTU_PORT = int(os.getenv("FUTU_PORT", "11111"))
FUTU_CACHE_TTL_SECONDS = max(1.0, float(os.getenv("FUTU_CACHE_TTL_SECONDS", "5")))
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "").strip()
SEC_CACHE_TTL_SECONDS = max(60.0, float(os.getenv("SEC_CACHE_TTL_SECONDS", "300")))
