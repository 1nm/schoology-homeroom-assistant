# Get Schoology Updates

Watches one Schoology homeroom and keeps a local copy of it:

- mails every new post to the parents, with an AI summary and translations;
- mirrors the course materials (PDFs, pages, class photo albums) to disk and mails
  what changed;
- serves the whole archive over MCP so an agent can search and read it.

## How a run works

1. **Session** — cookies from the last successful sign-in are loaded from
   `.schoology_cookies.json` and probed against `/home`. Only if they are gone or
   rejected does Chrome start up, sign in through Microsoft, switch into the child
   account, and hand its cookies back. Everything after this step is plain HTTP.
2. **Feed** — `/course/<id>/feed?filter=1` is fetched and parsed. No network calls
   happen during parsing.
3. **New posts only** — posts already listed in the state file are dropped here,
   *before* anything is downloaded or sent to a model.
4. **Per post** — expand "Show more", download attachments, extract PDF text,
   summarise, translate, mail, then record the post and flush the state file.
   A post is recorded only once the SMTP server has accepted the message, so a
   crash mid-run neither re-sends nor loses a post.

## Materials

Materials come from the Schoology REST API rather than the browser: request a key
at `https://<subdomain>.schoology.com/api` and set `SCHOOLOGY_API_CONSUMER_KEY`
and `SCHOOLOGY_API_CONSUMER_SECRET`.

A parent's key can read materials but **not** the course feed --
`OPTIONS /sections/<id>/updates` answers with an empty `Allow` header, while
`/documents` answers `Allow: GET` -- so posts still come from the scraped feed
and materials come from the API. Every attachment carries an md5, so a file counts
as changed only when its bytes do.

The first run mirrors everything and mails a list without attachments; later runs
mail only what changed, with the new files attached. Class photo albums are off by
default (`SYNC_ALBUMS`) because they run to several GB, and they are never listed
in the mail -- they are archived, not news.

## Asking questions about it (MCP)

```shell
pip install -r requirements-mcp.txt
python app/mcp_server.py --data-dir ~/homeroom --build-index   # once, and after a sync
python app/mcp_server.py --data-dir ~/homeroom                 # stdio
python app/mcp_server.py --data-dir ~/homeroom --http --host 10.0.0.4
```

The server retrieves and reads; it does not answer. `search()` blends exact keyword
matching with FAISS semantic search, `read_document()` returns a whole document,
and `get_file()`/`get_photos()` hand back the originals -- local path, a Schoology
URL that opens in a signed-in browser, and images inline. An agent can then iterate:
search, read the document in full, search again. That beats answering from whatever
a single top-k lookup returned, and it is why there is no `ask()` tool.

Semantic search also makes the archive answerable in a language the teacher never
wrote in: a Chinese question finds the right English post, which keyword search
cannot do at all.

`--host` must name an interface; binding `0.0.0.0` is refused, because the archive
holds photographs of other people's children. Over HTTP, pass `--token` (or set
`MCP_TOKEN`) and send it as `Authorization: Bearer <token>`; without one, anyone
who can reach the port can read everything.

For Claude Desktop on another machine, stdio travels over ssh:

```json
{"mcpServers": {"homeroom": {"command": "ssh", "args": [
  "user@10.0.0.4",
  "/path/to/.venv/bin/python /path/to/app/mcp_server.py --data-dir /path/to/data"]}}}
```

## Configuration

Copy `.env.example` to `.env` and fill it in. The five required variables are
`SCHOOLOGY_EMAIL`, `SCHOOLOGY_PASSWORD`, `SCHOOLOGY_SUBDOMAIN`,
`SUMMARY_SENDER_EMAIL` and `GOOGLE_APP_PASSWORD`; everything else has a default.

Set `HOMEROOM_COURSE_URL` (or `SCHOOLOGY_COURSE_ID`). Without it, the browser has
to find the course by link text, which breaks whenever the course is renamed or
is not on the page — during the summer holiday, for instance.

## Files kept in `DATA_DIR`

| File | Contents |
| --- | --- |
| `.sadc.conf` | ids of posts already mailed (bounded to the newest 500) |
| `.schoology_cookies.json` | the reusable session, mode `600` |
| `.error_notify.json` | throttling record so an identical failure is not mailed every run |
| `attachments/<post_id>/` | files downloaded for that post |
| `posts/<date>-<id>.md` | the teacher's original text, plus the summary that was mailed |
| `materials/` | the course materials, mirroring the folder tree, plus `index.json` |
| `materials/Photos/<album>/` | class photos at original resolution |
| `.semantic/`, `.textcache/` | the FAISS index and extracted PDF text |
| `error_screenshot.png` | the page the browser was on when a sign-in failed |

## Run

```shell
docker build -t gsu .
docker run --rm --env-file .env -v $PWD:/downloads gsu
```

`--dry-run` signs in, reads the feed and downloads attachments, but calls no
models, sends no mail, and records nothing. Use it to check a change or to find
out why a run is failing:

```shell
docker run --rm --env-file .env -v $PWD:/downloads gsu --dry-run
```

Locally, without Docker:

```shell
pip install -r requirements.txt
python app/main.py --dry-run
```

Exit codes: `0` success, `1` at least one post failed (an error mail was sent),
`2` the configuration is incomplete.

## Tests

```shell
pip install -r requirements-dev.txt
pytest
```

The tests cover the pure parts — feed parsing, date handling, attachment naming
and size limits, state migration and flushing, error throttling, and the
orchestration rules above. They make no network calls.
