from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class TelegramDeliveryContractTests(unittest.TestCase):
    def test_required_delivery_files_exist(self) -> None:
        for relative in (
            "docs/telegram_intelligence.md",
            "config/chats.example.yaml",
            "config/keywords.example.yaml",
            "Dockerfile.telegram",
            "docker-compose.telegram.yml",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)

    def test_readme_links_delivery_guide_and_preserves_boundary(self) -> None:
        readme = _read("README.md")
        self.assertIn("docs/telegram_intelligence.md", readme)
        self.assertIn("OpenAI 继续受宿主硬禁用策略约束", readme)
        self.assertIn("不等于真实 Telegram/TDLib", readme)

    def test_env_example_uses_fail_closed_defaults(self) -> None:
        env = _read(".env.example")
        for assignment in (
            "TELEGRAM_API_ID=",
            "TELEGRAM_API_HASH=",
            "TELEGRAM_PHONE=",
            "TELEGRAM_TDLIB_LIBRARY_PATH=",
            "TELEGRAM_TDLIB_DATABASE_ENCRYPTION_KEY=",
            "TELEGRAM_DATABASE_PATH=runtime/telegram/telegram.sqlite3",
            "TELEGRAM_HEALTH_HOST=127.0.0.1",
            "TELEGRAM_HISTORY_MAX_LIMIT=1000",
            "TELEGRAM_MAX_DATABASE_BYTES=4294967296",
            "TELEGRAM_SHUTDOWN_TIMEOUT_SECONDS=90",
            "ENABLE_AI=false",
            "ENABLE_ALERTS=false",
            "DOWNLOAD_MEDIA=false",
            "DRY_RUN=true",
            "TIMEZONE=Asia/Shanghai",
        ):
            self.assertIn(assignment, env)
        self.assertNotRegex(env, r"(?m)^TELEGRAM_BOT_TOKEN=\S+")
        self.assertNotRegex(env, r"(?m)^TELEGRAM_API_HASH=\S+")

    def test_chat_example_is_disabled_and_contains_only_contract_fields(self) -> None:
        chats = _read("config/chats.example.yaml")
        self.assertRegex(chats, r"(?m)^chats:\s*$")
        self.assertNotIn("enabled: true", chats)
        fields = set(
            re.findall(r"(?m)^(?:  - |    )([a-z][a-z0-9_]*):", chats)
        )
        self.assertEqual(
            fields,
            {"chat_id", "enabled", "name", "category", "priority", "trust_score"},
        )

    def test_keyword_example_has_nested_lists_and_required_roots(self) -> None:
        keywords = _read("config/keywords.example.yaml")
        for root in (
            "groups",
            "ticker_whitelist",
            "ambiguous_tickers",
            "ticker_context",
            "breaking_keywords",
        ):
            self.assertRegex(keywords, rf"(?m)^{root}:\s*$")
        self.assertRegex(
            keywords,
            r"(?m)^  [a-z_]+:\s*\n    keywords:\s*\n(?:      - .+\n)+"
            r"    regex: \[\]\n    tickers: \[\]$",
        )
        self.assertRegex(
            keywords,
            r"(?m)^ticker_whitelist:\s*\n(?:  [A-Z][A-Z0-9.-]*:\s*\n"
            r"(?:    - .+\n)+)+",
        )

    def test_docker_contract_copies_source_only_and_exposes_no_port(self) -> None:
        dockerfile = _read("Dockerfile.telegram")
        compose = _read("docker-compose.telegram.yml")
        self.assertIn("context: ./backend", compose)
        self.assertIn("dockerfile: ../Dockerfile.telegram", compose)
        self.assertIn("COPY --chown=telegram:telegram *.py /app/backend/", dockerfile)
        self.assertIn(
            "telegram_intelligence/*.py /app/backend/telegram_intelligence/",
            dockerfile,
        )
        self.assertNotRegex(dockerfile, r"(?mi)^\s*(?:ADD|COPY)\s+\.\s")
        self.assertNotRegex(dockerfile, r"(?mi)^\s*(?:ADD|COPY).*\.env")
        self.assertNotRegex(compose, r"(?m)^\s*ports:\s*$")
        self.assertIn('TELEGRAM_HEALTH_HOST: "127.0.0.1"', compose)
        self.assertIn(
            'TELEGRAM_MAX_DATABASE_BYTES: "${TELEGRAM_MAX_DATABASE_BYTES:-4294967296}"',
            compose,
        )
        self.assertIn('AI_STUDIO_DISABLED_PROVIDERS: "openai"', compose)
        self.assertIn('ENABLE_AI: "false"', compose)
        self.assertIn("telegram_intelligence_runtime", compose)
        self.assertIn("target: /app/config", compose)
        self.assertIn("target: /opt/tdlib/lib/libtdjson.so", compose)
        self.assertGreaterEqual(compose.count("read_only: true"), 3)
        for forbidden_mount in (".env.local", "tdlib-data:/", "files:/"):
            self.assertNotIn(forbidden_mount, compose)

    def test_guide_documents_cli_limits_recovery_and_verification_boundary(self) -> None:
        guide = _read("docs/telegram_intelligence.md")
        for command in ("login", "chats", "run", "history", "status"):
            self.assertIn(
                f"python -m backend.telegram_intelligence telegram {command}",
                guide,
            )
        for phrase in (
            "默认只取 100 条",
            "默认 1000",
            "100–10000",
            "Asia/Shanghai",
            "timezone-aware UTC",
            "WOULD_SEND",
            "collaboration_studio.sqlite3",
            "没有",
            "真实集成验证",
            "WAL/SHM",
            "loopback",
        ):
            self.assertIn(phrase, guide)


if __name__ == "__main__":
    unittest.main()
