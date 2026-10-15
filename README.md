# schoology-homeroom-assistant

A parent's assistant for one Schoology homeroom. It keeps a local archive of the
class and puts Claude on top of it:

- **sync** mails every new teacher post to the parents with an AI summary and
  translations, posts it to Slack, and mirrors the course materials and class photo
  albums to disk;
- **an MCP server** serves the archive (keyword + semantic search, whole documents,
  photos, photos of one child) to any MCP client;
- **a Slack bot** answers parents' questions about it ("what's the homework this
  week?", "show me photos of Alex from last week") in any language.

```
                 ┌──────────── data dir (~/homeroom): .env, posts/, materials/, .semantic/ ─────────────┐
 cron */30 ─▶ run.sh sync ──▶ Schoology ─▶ claude -p (summary, translations) ─▶ mail + Slack channel
 cron 04:47 ─▶ run.sh reindex ─▶ Qwen3-Embedding (local) ─▶ .semantic/qwen3-0.6b/
 cron * ─▶ run.sh serve  = MCP server, HTTP on 127.0.0.1:8848, bearer token, model loaded once
 cron * ─▶ run.sh bot ──▶ Slack (Socket Mode) ─▶ claude -p --mcp-config ─▶ MCP server ─┘
```

Everything runs on one machine, from one venv, out of one data directory, with one
`.env` in it. The LLM is Claude through the Claude Code CLI (`claude -p`) on a
Claude subscription; nothing calls OpenAI unless you ask it to.

## Deploy

What you need: Linux with Python 3.11, Chromium and chromedriver (only for the
occasional Schoology sign-in), the [Claude Code CLI](https://docs.claude.com/en/docs/claude-code),
a Claude subscription, a Gmail account with an app password, and a Slack workspace
where you can install an app.

**1. Code and venv**

```shell
git clone git@github.com:1nm/schoology-homeroom-assistant.git ~/git/schoology-homeroom-assistant
cd ~/git/schoology-homeroom-assistant
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo apt install chromium chromium-driver
```

**2. Claude.** Install the CLI, then create a long-lived token for this machine:

```shell
curl -fsSL https://claude.ai/install.sh | bash   # puts claude in ~/.local/bin
claude setup-token                               # prints sk-ant-oat01-...
```

The token goes into `.env` as `CLAUDE_CODE_OAUTH_TOKEN`. Without it the bot and
sync fall back to whatever `claude login` left on the machine, which expires.

**3. Data directory and `.env`.** All state lives here; the code directory holds none.

```shell
mkdir -p ~/homeroom && cp .env.example ~/homeroom/.env && chmod 600 ~/homeroom/.env
```

Fill in, at least:

| Variable | Where it comes from |
| --- | --- |
| `SCHOOLOGY_EMAIL`, `SCHOOLOGY_PASSWORD`, `SCHOOLOGY_SUBDOMAIN` | the parent's Schoology sign-in |
| `HOMEROOM_COURSE_URL`, `HOMEROOM_CLASS` | the homeroom course page; a short class label for subjects |
| `SUMMARY_SENDER_EMAIL`, `GOOGLE_APP_PASSWORD`, `SUMMARY_RECEIVER_EMAIL` | Gmail account + [app password](https://myaccount.google.com/apppasswords) |
| `SCHOOLOGY_API_CONSUMER_KEY`, `..._SECRET` | `https://<subdomain>.schoology.com/api` (materials mirror) |
| `CLAUDE_CODE_OAUTH_TOKEN` | step 2 |
| `MCP_TOKEN` | any random string: `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` |
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CHANNEL` | step 5 |
| `TIMEZONE`, `BOT_NAME`, `SCHOOL_NAME` | e.g. `Asia/Tokyo`, what the bot calls itself, the school |

**4. First sync and index.** Run from the data dir:

```shell
cd ~/homeroom
~/git/schoology-homeroom-assistant/run.sh sync --dry-run   # signs in, reads the feed, sends nothing
~/git/schoology-homeroom-assistant/run.sh sync --no-bcc    # first real run mirrors everything
~/git/schoology-homeroom-assistant/run.sh reindex
```

The first `reindex` downloads Qwen3-Embedding-0.6B (int8 ONNX, about 600 MB) into
`.models/` and embeds everything: about 35 minutes for 800 chunks on a 4-core
Intel N100. Later runs embed only text that changed.

**5. Slack app.** At <https://api.slack.com/apps> choose *Create New App → From a
manifest* and paste `slack/manifest.json` (rename the bot there if you like).
Install it to the workspace and copy the *Bot User OAuth Token* (`xoxb-…`) into
`SLACK_BOT_TOKEN`. Under *Basic Information → App-Level Tokens* create one with
`connections:write` and put it (`xapp-…`) in `SLACK_APP_TOKEN`. Then
`/invite @homeroom-bot` in the class channel, and set `SLACK_CHANNEL` to that
channel's id if sync should post there too.

**6. cron.** One copy of each long-running process, restarted within a minute if it
dies; all logs in the data dir, trimmed nightly in place (the running processes
keep their log files open, so never `mv` them):

```cron
# schoology-homeroom-assistant: everything runs from the data dir
*/30 * * * * cd /home/you/homeroom && /usr/bin/flock -n /tmp/homeroom-sync.lock /home/you/git/schoology-homeroom-assistant/run.sh sync --no-bcc >> cron.log 2>&1
* * * * *    cd /home/you/homeroom && /usr/bin/flock -n /tmp/homeroom-mcp.lock /home/you/git/schoology-homeroom-assistant/run.sh serve >> mcp.log 2>&1
* * * * *    cd /home/you/homeroom && /usr/bin/flock -n /tmp/homeroom-bot.lock /home/you/git/schoology-homeroom-assistant/run.sh bot >> slack-bot.log 2>&1
47 4 * * *   cd /home/you/homeroom && /home/you/git/schoology-homeroom-assistant/run.sh reindex >> cron.log 2>&1
17 4 * * *   for f in /home/you/homeroom/cron.log /home/you/homeroom/mcp.log /home/you/homeroom/slack-bot.log; do tail -n 5000 "$f" > "$f.tmp" && cat "$f.tmp" > "$f" && rm -f "$f.tmp"; done
```

`--no-bcc` mails only `SUMMARY_RECEIVER_EMAIL`; drop it to Bcc the parents in
`BCC_EMAILS`. Within two minutes `mcp.log` should say `Listening on
http://127.0.0.1:8848/mcp` and `slack-bot.log` `Bolt app is running!`.

## Use

### Parents, in Slack

- **@mention the bot** in the class channel, or **DM it**. It reacts with 👀
  while it works and answers in a thread, usually within 10-20 seconds.
- **Follow up in the thread** without mentioning it again; it reads the thread.
- **Ask in any language**; it answers in the language you wrote in. The teacher's
  English posts are found from Chinese or Japanese questions too.
- **Ask for photos**: "show me two photos from the newest album", "photos of Alex
  from last week". Photos come back as images in the thread.

Things people ask: "这周的作业是什么？", "When is the field trip and what do we
bring?", "先週のお便りを要約して", "What did the teacher say about reading logs?"

Answers come only from the archive, cite the post or material they used with its
Schoology link, and say so plainly when the archive has nothing.

### Maintainers

| Task | How |
| --- | --- |
| Watch the bot / server / sync | `tail -f ~/homeroom/slack-bot.log` / `mcp.log` / `cron.log` |
| Restart the bot or the server | `pkill -f 'app/slack_bot.py'` or `pkill -f 'mcp_server.py --data-dir .* --http'`; cron starts it again within a minute |
| Deploy a code change | `git pull`, `.venv/bin/pip install -r requirements.txt` if it changed, then restart both as above; sync picks it up on its next run |
| Check a sync without side effects | `cd ~/homeroom && run.sh sync --dry-run` |
| Rebuild the search index now | `cd ~/homeroom && run.sh reindex` (the server reads the new index on its next search) |
| Rotate the Claude token | `claude setup-token`, replace `CLAUDE_CODE_OAUTH_TOKEN` in `.env`, restart the bot |
| Teach it a child's face | see [Finding one child in the albums](#finding-one-child-in-the-albums) |

**Using the archive from Claude Code or Claude Desktop.** The MCP server listens on
127.0.0.1 only. On the same machine:

```shell
claude mcp add --transport http homeroom http://127.0.0.1:8848/mcp \
  --header "Authorization: Bearer $MCP_TOKEN"
```

From another machine, forward the port first (`ssh -N -L 8848:127.0.0.1:8848
you@server`) and use the same URL.

## How it works

### A sync run

1. **Session** — cookies from the last successful sign-in are loaded from
   `.schoology_cookies.json` and probed against `/home`. Only if they are gone or
   rejected does Chrome start up, sign in through Microsoft, switch into the child
   account, and hand its cookies back. Everything after this step is plain HTTP.
2. **Feed** — `/course/<id>/feed?filter=1` is fetched and parsed. No network calls
   happen during parsing.
3. **New posts only** — posts already listed in the state file are dropped here,
   *before* anything is downloaded or sent to a model.
4. **Per post** — expand "Show more", download attachments, extract PDF text,
   summarise and translate (`claude -p`, no tools: opus for the summary, sonnet for
   the translations), mail, then record the post and flush the state file. A post
   is recorded only once the SMTP server has accepted the message, so a crash
   mid-run neither re-sends nor loses a post. Slack is posted last; a Slack failure
   is logged, not retried.

Exit codes: `0` success, `1` at least one post failed (an error mail was sent),
`2` the configuration is incomplete.

### Materials

Materials come from the Schoology REST API rather than the browser. A parent's key
can read materials but **not** the course feed -- `OPTIONS /sections/<id>/updates`
answers with an empty `Allow` header, while `/documents` answers `Allow: GET` -- so
posts still come from the scraped feed and materials come from the API. Every
attachment carries an md5, so a file counts as changed only when its bytes do.

The first run mirrors everything and mails a list without attachments; later runs
mail only what changed, with the new files attached. Class photo albums are off by
default (`SYNC_ALBUMS`) because they run to several GB, and they are never listed
in the mail -- they are archived, not news.

### The MCP server

The server retrieves and reads; it does not answer. `search()` blends exact keyword
matching with FAISS semantic search, `read_document()` returns a whole document,
and `get_file()`/`get_photos()` hand back the originals -- local path, a Schoology
URL that opens in a signed-in browser, and images inline. An agent can then iterate:
search, read the document in full, search again. That beats answering from whatever
a single top-k lookup returned, and it is why there is no `ask()` tool.

Semantic search uses Qwen3-Embedding-0.6B, run locally on onnxruntime. It is
multilingual, so a Chinese question finds the right English post, which keyword
search cannot do at all. Each embedding backend keeps its own index directory under
`.semantic/`, and embeddings are cached by chunk hash, so a reindex only pays for
text that changed. If the model or index is missing, `search()` falls back to
keywords instead of failing.

`run.sh serve` binds 127.0.0.1 and requires `MCP_TOKEN` as a bearer token. Binding
`0.0.0.0` is refused, because the archive holds photographs of other people's
children. The server also still speaks stdio (`python app/mcp_server.py --data-dir
~/homeroom`) for clients that spawn it themselves.

### The Slack bot

Each question becomes one `claude -p` run: the thread so far as the prompt, a
system prompt with today's date in `TIMEZONE`, the MCP server as its only tools
(`--tools ""`, `--strict-mcp-config`, `--setting-sources ""`, so nothing else on the
machine is reachable), and at most `BOT_MAX_TURNS` tool calls. Photos the tools
return are picked out of the `stream-json` output and uploaded to the thread. The
MCP URL and token go to Claude Code in `.mcp-bot.json` (mode 600), not on the
command line.

### Finding one child in the albums

The albums are photographed for the whole class. `faces.py` runs a face detector over
every photo locally (insightface on the CPU; nothing leaves the machine) and learns a
child from a few photos a parent points at -- the face that recurs across them:

```shell
cd ~/homeroom
~/git/schoology-homeroom-assistant/.venv/bin/python ~/git/schoology-homeroom-assistant/app/faces.py --data-dir . scan
~/git/schoology-homeroom-assistant/.venv/bin/python ~/git/schoology-homeroom-assistant/app/faces.py --data-dir . learn alex "Week 4 (:7,42" "Week 3 (:16"
~/git/schoology-homeroom-assistant/.venv/bin/python ~/git/schoology-homeroom-assistant/app/faces.py --data-dir . match alex --sheet review.jpg
```

Numbers refer to a review contact sheet (photos of an album sorted by filename,
1-based). After that, every album sync checks the new photos for each learned
person, records the result in `.faces/matches.json`, and posts the hits to Slack as
photos (reframed around the child when they were only in the background). The bot
finds them through `list_people()`, `get_photos_of(person)` and
`get_photos(..., person=)`. `FACE_THRESHOLD` (default 0.45) trades misses for false
hits.

## Configuration reference

All settings live in the data dir's `.env`; `.env.example` lists every one with its
default. The groups:

| Group | Variables |
| --- | --- |
| Schoology | `SCHOOLOGY_EMAIL`, `SCHOOLOGY_PASSWORD`, `SCHOOLOGY_SUBDOMAIN`, `HOMEROOM_COURSE_URL` or `SCHOOLOGY_COURSE_ID`, `HOMEROOM_CLASS`, `SCHOOLOGY_API_CONSUMER_KEY`/`_SECRET` |
| Mail | `SUMMARY_SENDER_EMAIL`, `GOOGLE_APP_PASSWORD`, `SUMMARY_RECEIVER_EMAIL`, `BCC_EMAILS` |
| Claude | `CLAUDE_CODE_OAUTH_TOKEN`, `LLM_PROVIDER` (claude), `SUMMARY_MODEL` (opus), `TRANSLATION_MODEL` (sonnet), `TRANSLATION_LANGUAGES` (Japanese,Chinese), `CLAUDE_BIN` |
| Search | `EMBEDDING_BACKEND` (qwen3), `EMBED_THREADS` (3) |
| MCP server | `MCP_TOKEN` |
| Slack bot | `SLACK_APP_TOKEN`, `BOT_NAME`, `SCHOOL_NAME`, `TIMEZONE`, `BOT_MODEL` (sonnet), `BOT_MAX_TURNS` (8), `BOT_TIMEOUT` (600 s), `THREAD_HISTORY` (20), `MCP_URL` |
| Slack channel posts | `SLACK_BOT_TOKEN`, `SLACK_CHANNEL`, `SLACK_MENTIONS` |
| Materials and albums | `MATERIALS_SKIP_FOLDERS`, `MAX_MATERIAL_MB`, `SYNC_ALBUMS`, `ALBUM_ORIGINALS`, `MAX_ALBUM_MB` |
| Faces | `FACE_THRESHOLD`, `FACE_MIN_PROMINENCE`, `FACE_MIN_SIZE`, `FACE_FRAME_BELOW`, `FACE_THREADS` |

`LLM_PROVIDER=openai` and `EMBEDDING_BACKEND=openai` switch back to the OpenAI API
(with `OPENAI_API_KEY`); the Slack bot is Claude only.

### Files kept in the data dir

| File | Contents |
| --- | --- |
| `.env` | all settings and secrets, mode `600` |
| `.sadc.conf` | ids of posts already mailed (bounded to the newest 500) |
| `.schoology_cookies.json` | the reusable session, mode `600` |
| `.error_notify.json` | throttling record so an identical failure is not mailed every run |
| `attachments/<post_id>/` | files downloaded for that post |
| `posts/<date>-<id>.md` | the teacher's original text, plus the summary that was mailed |
| `materials/` | the course materials, mirroring the folder tree, plus `index.json` |
| `materials/Photos/<album>/` | class photos at original resolution |
| `.semantic/qwen3-0.6b/`, `.textcache/` | the FAISS index with its embedding cache, and extracted PDF text |
| `.models/` | the downloaded embedding model |
| `.faces/` | face embeddings, learned people, matches |
| `.mcp-bot.json` | the bot's MCP client config (with the token), mode `600` |
| `cron.log`, `mcp.log`, `slack-bot.log` | logs, trimmed nightly |
| `error_screenshot.png` | the page the browser was on when a sign-in failed |

## Troubleshooting

| Symptom | Look at |
| --- | --- |
| The bot says "Sorry, something went wrong" | `slack-bot.log`. `Credit balance is too low` means an `ANTHROPIC_API_KEY` reached the CLI; a usage-limit message means the subscription's 5-hour window is used up and it recovers on its own; `Not logged in` / 401 means `CLAUDE_CODE_OAUTH_TOKEN` is missing or expired. |
| The bot answers but finds nothing, or `MCP server not connected` in its log | `mcp.log`: is the server up on 8848, and does `MCP_TOKEN` match? `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8848/mcp` should say `401`. |
| Chinese or Japanese questions miss obvious posts | `mcp.log` says `answering from keywords alone` or `search falls back to keywords`: the model or index is missing. Run `run.sh reindex`. |
| `serve` exits with `address already in use` | another copy holds 8848: `ss -ltnp 'sport = :8848'`. |
| The bot does not react at all | `slack-bot.log` has no `Bolt app is running!`: check `SLACK_APP_TOKEN`, and that the app is invited to the channel. |
| No summary mails | `cron.log`; run `run.sh sync --dry-run` by hand from the data dir. Exit code 2 means a required setting is missing. |

## Tests

```shell
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

The tests cover the pure parts — feed parsing, date handling, attachment naming
and size limits, state migration and flushing, error throttling, and the
orchestration rules above. They make no network calls.
