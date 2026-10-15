"""Slack bot: parents ask questions about the homeroom archive, Claude answers.

Each question runs one `claude -p` (Claude Code CLI) with the archive's MCP server
handed over via --mcp-config, so Claude Code runs the tool loop itself; tool
results, photos included, are read back from its stream-json output. The MCP
server runs on its own as a long-lived HTTP service (`run.sh serve`), so the
embedding model is loaded once, not per question.

Socket Mode, so nothing needs to be reachable from the internet. Conversation
memory is the Slack thread itself. Run from the data dir; settings come from its
.env (see .env.example, "Slack bot").
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import find_dotenv, load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

load_dotenv(find_dotenv(usecwd=True))

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("slack_bot")

SYSTEM_PROMPT = """You are {bot_name}, the assistant for one homeroom class{at_school}. \
Parents ask you about the teacher's posts, weekly homework pages, course materials \
(PDFs, pages) and class photo albums. Everything you know comes from the tools; never \
invent dates, tasks or files.

How to work:
- Start with search() (it understands questions in any language even when the teacher \
writes in English), then read_document() to read the best hit in full before \
answering. For "latest" or "this week" questions, use list_posts() / list_materials() \
and pick by date. Use get_photos() when someone asks to see pictures. When they ask for \
pictures of a particular child, use get_photos_of(person) (all albums, newest first) or \
get_photos(album, person=...) for one album; list_people() tells you who can be found. \
Album names usually carry the week or its dates; match relative dates such as \
"last week" against them.
- Cite what you used: the post date and author, or the material title, and include the \
Schoology URL when a tool returns one.
- Answer in the language the user wrote in. Be concise; a parent is reading this on a \
phone. Write standard Markdown: **bold**, "- " bullet lists, [text](url) links. No \
headings, no tables, no code blocks.
- If the archive has nothing on the topic, say so plainly.
The tools are the mcp__homeroom__* tools; search() above means mcp__homeroom__search, \
and so on.
"""

OUT_OF_TURNS = (
    "I looked through a lot of the archive but couldn't settle on an answer — "
    "try a narrower question."
)


@dataclass
class Answer:
    text: str
    images: list[tuple[bytes, str]] = field(default_factory=list)  # (bytes, mime)


def find_claude() -> str:
    # cron has a bare PATH; the installer puts claude in ~/.local/bin.
    for candidate in (
        os.environ.get("CLAUDE_BIN"),
        shutil.which("claude"),
        str(Path.home() / ".local" / "bin" / "claude"),
    ):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    raise FileNotFoundError("claude CLI not found; install it or set CLAUDE_BIN")


class Agent:
    def __init__(self) -> None:
        self.claude_bin = find_claude()
        self.model = os.environ.get("BOT_MODEL", "sonnet")
        self.max_turns = int(os.environ.get("BOT_MAX_TURNS", "8"))
        self.timeout = int(os.environ.get("BOT_TIMEOUT", "600"))
        tz = os.environ.get("TIMEZONE", "").strip()
        if not tz:
            logger.warning("TIMEZONE is not set; dates are resolved in UTC")
        self.tz = ZoneInfo(tz or "UTC")
        school = os.environ.get("SCHOOL_NAME", "").strip()
        self.bot_name = os.environ.get("BOT_NAME", "").strip() or "Homeroom Bot"
        self.system_prompt = SYSTEM_PROMPT.format(
            bot_name=self.bot_name, at_school=f" at {school}" if school else ""
        )
        self.mcp_config = self._write_mcp_config()
        logger.info("LLM: claude -p (%s) / %s; MCP %s", self.claude_bin, self.model,
                    os.environ.get("MCP_URL", "http://127.0.0.1:8848/mcp"))

    @staticmethod
    def _write_mcp_config() -> str:
        """The MCP token goes in a 0600 file, not on the command line where `ps` shows it."""
        server = {"type": "http", "url": os.environ.get("MCP_URL", "http://127.0.0.1:8848/mcp")}
        if os.environ.get("MCP_TOKEN"):
            server["headers"] = {"Authorization": f"Bearer {os.environ['MCP_TOKEN']}"}
        path = Path.cwd() / ".mcp-bot.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"mcpServers": {"homeroom": server}}, f)
        os.chmod(path, 0o600)
        return str(path)

    def _prompt(self, history: list[dict]) -> str:
        """`claude -p` takes one prompt, so earlier thread turns become a transcript."""
        *earlier, current = history
        if not earlier:
            return current["content"]
        transcript = "\n\n".join(
            f"[{self.bot_name if t['role'] == 'assistant' else 'parent'}]\n{t['content']}"
            for t in earlier
        )
        return (
            "Earlier messages in this Slack thread, oldest first:\n\n"
            f"{transcript}\n\n"
            "Now reply to the parent's newest message:\n\n"
            f"{current['content']}"
        )

    def answer(self, history: list[dict]) -> Answer:
        """history: [{"role": "user"|"assistant", "content": str}, ...], newest last."""
        today = datetime.now(self.tz).strftime("%A, %Y-%m-%d")
        system = f"{self.system_prompt}\nToday is {today} ({self.tz.key})."
        argv = [
            self.claude_bin,
            "--print",
            "--output-format", "stream-json",   # tool results (with images) are in the stream
            "--verbose",                         # required for stream-json under --print
            "--model", self.model,
            "--system-prompt", system,
            "--mcp-config", self.mcp_config,
            "--strict-mcp-config",               # not the machine's other MCP servers
            "--tools", "",                       # no Bash/Read/Edit..., only the MCP tools
            "--allowedTools", "mcp__homeroom__*",
            "--max-turns", str(self.max_turns),
            "--setting-sources", "",             # ignore ~/.claude settings, hooks, plugins
            "--no-session-persistence",
        ]
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)  # allow running from inside a Claude Code session
        # The CLI prefers an API key over the subscription login / OAuth token.
        env.pop("ANTHROPIC_API_KEY", None)

        prompt = self._prompt(history)
        logger.info("question: %s", prompt[-200:].replace("\n", " "))
        try:
            proc = subprocess.run(
                argv, input=prompt, env=env, capture_output=True, text=True,
                timeout=self.timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"claude -p timed out after {self.timeout}s") from exc

        images: list[tuple[bytes, str]] = []
        result: dict | None = None
        for line in proc.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                down = [s for s in event.get("mcp_servers", []) if s.get("status") != "connected"]
                if down:
                    logger.warning("MCP server not connected: %s", down)
            elif kind == "assistant":
                for block in event["message"].get("content", []):
                    if block.get("type") == "tool_use":
                        logger.info(
                            "tool %s %s", block.get("name"),
                            json.dumps(block.get("input"), ensure_ascii=False)[:200],
                        )
            elif kind == "user":
                # Tool results come back as user turns; images are base64 blocks.
                for block in event["message"].get("content", []):
                    if block.get("type") != "tool_result":
                        continue
                    content = block.get("content")
                    for part in content if isinstance(content, list) else []:
                        source = part.get("source") or {}
                        if part.get("type") == "image" and source.get("data"):
                            images.append((base64.b64decode(source["data"]), source["media_type"]))
            elif kind == "result":
                result = event

        if result is None:
            raise RuntimeError(
                f"claude -p exited {proc.returncode} without a result: "
                f"{proc.stderr.strip()[-500:] or proc.stdout.strip()[-500:]}"
            )
        logger.info(
            "claude -p: %s, %s turns, $%.4f",
            result.get("subtype"), result.get("num_turns"), result.get("total_cost_usd") or 0,
        )
        if result.get("subtype") == "error_max_turns":
            return Answer(text=OUT_OF_TURNS, images=images)
        if result.get("is_error"):
            raise RuntimeError(f"claude -p failed: {str(result.get('result'))[:500]}")
        return Answer(text=result.get("result") or "", images=images)


# --- Slack -----------------------------------------------------------------------

THREAD_HISTORY = int(os.environ.get("THREAD_HISTORY", "20"))


def build_app(agent: Agent) -> App:
    app = App(token=os.environ["SLACK_BOT_TOKEN"])
    bot_user_id = app.client.auth_test()["user_id"]

    def clean(text: str) -> str:
        return re.sub(rf"<@{bot_user_id}>\s*", "", text or "").strip()

    def history_of(client, channel: str, thread_ts: str | None, current_ts: str) -> list[dict]:
        """Earlier turns of this thread, oldest first, without the current message."""
        if not thread_ts:
            return []
        replies = client.conversations_replies(channel=channel, ts=thread_ts, limit=THREAD_HISTORY)
        turns = []
        for m in replies["messages"]:
            if m.get("ts") == current_ts or m.get("subtype") not in (None, "bot_message"):
                continue
            role = "assistant" if m.get("user") == bot_user_id or m.get("bot_id") else "user"
            text = clean(m.get("text", ""))
            if text:
                turns.append({"role": role, "content": text})
        return turns[-THREAD_HISTORY:]

    def post(client, channel: str, thread_ts: str, text: str) -> None:
        # A "markdown" block renders real Markdown; plain mrkdwn ignores *bold* next
        # to CJK punctuation. The text field is the notification/fallback copy.
        try:
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=text,
                blocks=[{"type": "markdown", "text": text[:12000]}],
            )
        except Exception:  # noqa: BLE001 - never lose the answer over rendering
            logger.exception("markdown block rejected, sending plain text")
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)

    def bot_in_thread(client, channel: str, thread_ts: str) -> bool:
        replies = client.conversations_replies(channel=channel, ts=thread_ts, limit=THREAD_HISTORY)
        return any(
            m.get("user") == bot_user_id or f"<@{bot_user_id}>" in m.get("text", "")
            for m in replies["messages"]
        )

    def respond(event: dict, client) -> None:
        channel, ts = event["channel"], event["ts"]
        thread_ts = event.get("thread_ts") or ts
        question = clean(event.get("text", ""))
        if not question:
            return

        history = history_of(client, channel, event.get("thread_ts"), ts)
        history.append({"role": "user", "content": question})
        try:
            client.reactions_add(channel=channel, timestamp=ts, name="eyes")
        except Exception:  # noqa: BLE001 - a reaction is decoration
            pass

        try:
            answer = agent.answer(history)
            text = answer.text.strip() or "I couldn't find anything on that."
            post(client, channel, thread_ts, text)
            for i, (data, mime) in enumerate(answer.images, 1):
                ext = mime.split("/")[-1].replace("jpeg", "jpg")
                client.files_upload_v2(
                    channel=channel, thread_ts=thread_ts, content=data,
                    filename=f"photo-{i}.{ext}", title=f"photo {i}",
                )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to answer in %s", channel)
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text="Sorry, something went wrong on my side. Please try again in a minute.",
            )
        finally:
            try:
                client.reactions_remove(channel=channel, timestamp=ts, name="eyes")
            except Exception:  # noqa: BLE001
                pass

    @app.event("app_mention")
    def on_mention(event, client):
        respond(event, client)

    @app.event("message")
    def on_message(event, client):
        if event.get("bot_id") or event.get("subtype"):
            return
        if event.get("channel_type") == "im":
            respond(event, client)
            return
        # In a channel, a reply inside a thread the bot is already part of counts as
        # addressed to it, no @mention needed. Mentions are handled by app_mention.
        thread_ts = event.get("thread_ts")
        if (
            event.get("channel_type") == "channel"
            and thread_ts and thread_ts != event.get("ts")
            and f"<@{bot_user_id}>" not in event.get("text", "")
            and bot_in_thread(client, event["channel"], thread_ts)
        ):
            respond(event, client)

    return app


def main() -> None:
    agent = Agent()
    app = build_app(agent)
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
