# Get Schoology Updates

Watches a Schoology course feed and mails every new homeroom post to the parents,
with an AI summary and translations attached below the original text.

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
