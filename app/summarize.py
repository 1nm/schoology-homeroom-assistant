"""Turning a homeroom post into the summary block the email carries.

Two backends, picked by `LLM_PROVIDER`: "claude" runs the Claude Code CLI
(`claude -p`) on the Claude subscription, authenticated by `claude login` or a
CLAUDE_CODE_OAUTH_TOKEN; "openai" calls the API with OPENAI_API_KEY.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

import markdown2

logger = logging.getLogger(__name__)

CLAUDE_TIMEOUT = 300  # seconds per call
CLAUDE_SYSTEM_PROMPT = (
    "You are a careful writing assistant. Follow the instructions in the message "
    "exactly and reply with only the requested text: no preamble, no commentary."
)

PROMPT_TEMPLATE = """
You are summarising a homeroom update sent by an elementary school teacher to the
parents of the class. The reader is a parent skimming on a phone who needs to know
two things: what they have to do, and by when.

The update was posted on {posted_on}. Use that to turn a relative reference into a real
date, written once in parentheses after the teacher's own wording, and never repeating
that wording inside the parentheses.

Only do this when the update names a weekday that means one particular day, such as the
days of a single week's homework. Add nothing at all in these three cases:
- The update already gives the date ("August 24th-28th" needs no help).
- The weekday repeats ("every Monday and Wednesday", "each Friday") -- it names no one day.
- The reference is vague ("the week after next", "later this term", "soon").
Getting one of these wrong sends a parent looking on the wrong day, which is worse than
not resolving it at all.

Rules:
- Use only what the update says. Never add, guess, or generalise. If a detail is
  vague in the original, keep it vague.
- Copy every hard fact exactly: dates, times, deadlines, amounts of money, items to
  bring, page or unit numbers, links, and people's names.
- "Action Items" are things the parent or child must actually do, including
  homework and anything to sign, send, pay, pack, or attend. Keep day-by-day lists
  day by day; do not merge them into one line. Put the deadline in bold when the
  update states one; when it does not, say nothing about the deadline at all.
- "Information" is everything else worth knowing: at most six items, each at most
  three sub-points. Merge related points, and leave out encouragement, mission
  statements, and anything that tells the parent nothing they can act on or note
  down. Never drop a concrete fact, though -- a date, time, code, link, cost or
  requirement stays even if everything around it goes.
- The summary must be substantially shorter than the update. If the update is mostly
  narrative or encouragement, one or two Information items is the right length.
- If a section has nothing in it, write a single sub-point "- None".
- Write any letter or sound written in angle brackets, such as <sh> or <ch>, in
  double quotes instead: "sh", "ch".
- No strikethrough anywhere.

Output format -- markdown only, no code fences, no preamble:
- A title "AI Summary" with a single '#'.
- Two sections, "## Action Items" and "## Information", in that order.
- Under each, numbered items (1., 2., ...), each a bolded name followed by a colon.
- Details under an item are unnumbered sub-points starting with "-".

Example:
```
# AI Summary
## Action Items
1. **Homework this week:**
    - Monday: 20 minutes reading, plus the maths packet (**due Friday**)
    - Tuesday: maths activity
2. **Animal Book Presentation Sign-Up:**
    - Email the teacher three slots from June 4, 5, 6 (8:10-8:25 or 15:30-15:45).
    - For a morning slot, come to school with your child.

## Information
1. **Unit 1 - How We Express Ourselves:**
    - Storytelling through fables, folklore and fairytales.
```

Update from the homeroom teacher:
```
{text}
```

Summary in markdown format, no triple backticks:
"""

TRANSLATION_PROMPT_TEMPLATE = """
    Translate the following content into {language}, keep the original markdown formatting.
    Translate for parents: natural, plain, and unambiguous about dates and deadlines.
    Translate every line, including list items and sub-points. Leave nothing in English
    except people's names, programme and product names such as "Reading Eggs", and links.
    Keep dates, times and numbers accurate, but write them the way {language} normally does,
    using half-width (ASCII) digits. Translate a date range as a range, not as two separate dates.
    Use the school subject terminology that {language} schools actually use rather than a
    word-by-word rendering -- for example, in Chinese "number line" is 数轴 and "hundreds"
    as a place value is 百位.
    For English to Japanese, translate "AI Summary" to "AI 概要", "Action Items" to "アクションアイテム", "Information" to "情報", "fact families" to "ファクトファミリー"
    For English to Chinese, translate "AI Summary" to "AI 总结", "Action Items" to "行动项目", "Information" to "信息", "fact families" to "fact families".

    ```
    {content}
    ```

    Translation in markdown format, no triple backticks:
"""

MAX_ATTEMPTS = 3


def summarize(
    text: str, model: str, posted_on: str = "an unstated date", provider: str = "claude"
) -> str:
    return _invoke(
        PROMPT_TEMPLATE, {"text": text, "posted_on": posted_on}, model, provider,
        f"summary ({provider}/{model})",
    )


def translate(markdown_content: str, language: str, model: str, provider: str = "claude") -> str:
    return _invoke(
        TRANSLATION_PROMPT_TEMPLATE, {"content": markdown_content, "language": language},
        model, provider, f"{language} translation ({provider}/{model})",
    )


# --- backends -----------------------------------------------------------------

def _complete(template: str, payload: dict, model: str, provider: str) -> str:
    if provider == "claude":
        return _claude(template.format(**payload), model)
    if provider == "openai":
        return _openai(template, payload, model)
    raise ValueError(f"LLM_PROVIDER must be claude or openai, not {provider!r}")


def _openai(template: str, payload: dict, model: str) -> str:
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_openai import ChatOpenAI

    chain = ChatPromptTemplate.from_template(template) | ChatOpenAI(model=model) | StrOutputParser()
    return chain.invoke(payload)


def _claude_bin() -> str:
    # cron has a bare PATH; the installer puts claude in ~/.local/bin.
    for candidate in (
        os.environ.get("CLAUDE_BIN"),
        shutil.which("claude"),
        str(Path.home() / ".local" / "bin" / "claude"),
    ):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    raise FileNotFoundError("claude CLI not found; install it or set CLAUDE_BIN")


def _claude(prompt: str, model: str) -> str:
    """One `claude -p` call: no tools, none of this machine's settings, text in, text out."""
    argv = [
        _claude_bin(), "--print",
        "--output-format", "json",
        "--model", model,
        "--system-prompt", CLAUDE_SYSTEM_PROMPT,
        "--tools", "",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--no-session-persistence",
    ]
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)         # allow running from inside a Claude Code session
    env.pop("ANTHROPIC_API_KEY", None)  # the CLI would prefer a key over the subscription
    proc = subprocess.run(
        argv, input=prompt, env=env, capture_output=True, text=True,
        timeout=CLAUDE_TIMEOUT, check=False,
    )
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"claude -p exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[-500:]}"
        ) from exc
    if result.get("is_error") or not result.get("result"):
        raise RuntimeError(f"claude -p failed ({result.get('subtype')}): {str(result.get('result'))[:500]}")
    logger.info("claude -p: %s turns, $%.4f", result.get("num_turns"), result.get("total_cost_usd") or 0)
    return result["result"]


def markdown_to_html(markdown_content: str) -> str:
    # Teachers write phonics as <sh>, <ch>, <x>; without escaping, markdown2 hands
    # them to the mail client as unknown HTML tags and they vanish from the summary.
    return markdown2.markdown(markdown_content, safe_mode="escape")


def _invoke(template: str, payload: dict, model: str, provider: str, description: str) -> str:
    """Model calls fail transiently often enough that one retry loop pays for itself."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            logger.info("Requesting %s (attempt %d/%d)", description, attempt, MAX_ATTEMPTS)
            return _complete(template, payload, model, provider)
        except Exception as exc:
            if attempt == MAX_ATTEMPTS:
                raise
            delay = 2 ** attempt
            logger.warning("%s failed (%s), retrying in %ds", description, exc, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")
