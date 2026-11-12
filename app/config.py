from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # A variable left blank in .env (as in .env.example) means "use the
    # default below", not "set it to an empty string".
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    anthropic_api_key: str = ""
    webhook_secret: str = ""
    ticktick_api_token: str = ""
    notion_token: str = ""
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"

    # Public origin of this server; the Notion MCP OAuth callback lands here.
    public_base_url: str = ""
    # Notion hosted MCP via the Anthropic MCP connector. Needs a token from
    # scripts/notion_oauth.py; without one the agent uses the legacy tools.
    notion_mcp_enabled: bool = True
    notion_mcp_url: str = "https://mcp.notion.com/mcp"

    # Set RING_TZ in .env to your own IANA timezone, e.g. America/Chicago.
    ring_tz: str = "UTC"
    model: str = "claude-sonnet-4-6"
    escalation_model: str = "claude-fable-5"
    max_agent_turns: int = 8

    # Directory holding captures.db, audio/, and context/. Defaults to the
    # repo root in dev; /opt/ring-agent in production via .env.
    data_dir: Path = Path(__file__).resolve().parent.parent

    google_client_secret_file: str = "google_client_secret.json"
    google_token_file: str = "google_token.json"
    google_calendar_id: str = "primary"

    # Notion page (shared with NOTION_TOKEN) that Undo writes its log to.
    notion_undo_page_id: str = ""
    # Tools that never run without an Approve tap on the phone. Softer rules
    # (bulk changes, ...) live in context/CONTEXT.md "Needs my approval".
    approval_required_tools: str = (
        "ticktick_delete_task,calendar_delete_event,notion_trash_page"
    )

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.ring_tz)

    @property
    def approval_required(self) -> set[str]:
        return {t.strip() for t in self.approval_required_tools.split(",") if t.strip()}

    @property
    def db_path(self) -> Path:
        return self.data_dir / "captures.db"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def context_dir(self) -> Path:
        return self.data_dir / "context"


settings = Settings()
