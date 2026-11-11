# FB Bot: travel group helper

## Purpose
Every hour, the bot reads new posts in one or more Facebook travel groups, finds people who are visiting or asking travel questions, and comments a short, natural reply from your own Facebook account.

How one run works:
1. **Scan.** Your Chrome opens the group sorted by "New posts" and scrolls. The bot reads the post data Facebook loads and skips posts it has already seen.
2. **Draft.** Claude reads each new post. Ads, sales posts, posts older than 48 hours and photo-only posts are ignored. For the rest, Claude writes a reply of one or two sentences, in the post's language, using facts from `knowledge.md`.
3. **Approve.** With `auto_post: false`, every draft waits for `python bot.py review`. With `auto_post: true`, drafts rated `high` confidence are approved on their own. Lower-confidence drafts still wait for review.
4. **Post.** Approved comments are typed into the post's comment box, at most 5 per run and 15 per day, with a 90 to 240 second pause between them.

## Trigger
- Hourly: macOS launchd (`scripts/com.fbbot.hourly.plist`) or `python bot.py loop` running in a terminal.
- Manual: any command below.

## Steps

### One-time setup
```bash
git clone https://github.com/devtayyab/FbBot.git && cd FbBot
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt   # Python 3.10 or newer
cp config.example.yaml config.yaml     # set groups, destination, auto_post
cp .env.example .env                   # paste your Anthropic API key
# edit knowledge.md: the facts you want replies to use
.venv/bin/python bot.py login          # log in to your Facebook account in the Chrome window, then press Enter
```

### Test before going live
```bash
.venv/bin/python bot.py try "Coming next week with my kids, where should we stay?"   # check tone, no Facebook
.venv/bin/python bot.py scan --debug     # real scan, drafts only, nothing is posted
.venv/bin/python bot.py review           # approve / edit / skip
.venv/bin/python bot.py post --dry-run   # finds the comment box, types nothing
.venv/bin/python bot.py post             # posts approved comments
```

### Run every hour
Option A, launchd (runs while you are logged in to your Mac and it is awake):
```bash
# run from the project folder; fills in its path
sed "s#__PROJECT_DIR__#$PWD#g" scripts/com.fbbot.hourly.plist > ~/Library/LaunchAgents/com.fbbot.hourly.plist
launchctl load ~/Library/LaunchAgents/com.fbbot.hourly.plist
# stop: launchctl unload ~/Library/LaunchAgents/com.fbbot.hourly.plist
```
Option B, terminal: `.venv/bin/python bot.py loop` (Ctrl+C stops it).

### Daily use
- `python bot.py status`: counts per status and recent failures.
- `python bot.py review`: clear drafts that were not auto-approved. A macOS notification tells you when drafts are waiting.
- Logs: `data/logs/YYYY-MM-DD.log`.

## Systems and access
| What | Where | Notes |
|---|---|---|
| Facebook account | your own account, logged in once with `bot.py login` | Session is stored in `data/fb_profile/`. Treat that folder like a password. |
| Browser | your installed Google Chrome, using a separate bot profile | Your normal Chrome profile is not touched. |
| Claude API | `ANTHROPIC_API_KEY` in `.env` | Model `claude-opus-5-5`, low effort. Estimate: about 1 cent per new post, based on list prices of $4 / $20 per million input / output tokens. |
| Database | `data/bot.db` (SQLite) | Posts, drafts, statuses, and authors already seen. |

## Interfaces
- **In:** Facebook group pages (`?sorting_setting=CHRONOLOGICAL`) and the JSON data they load (`/api/graphql/`).
- **Out:** comments on group posts, typed into Facebook's comment box.
- **Sent to Claude:** the post text only. Names and profile IDs stay on your Mac.

## Known issues
- **No official API.** Meta removed the Groups API in April 2024, so the bot drives a real browser. Facebook's terms do not allow automated collection or posting. The account can be rate-limited, checkpointed or restricted. Keep the limits low and reply only where you add value.
- **Facebook changes its page data.** If a scan finds 0 posts, the raw data is saved to `data/debug/<time>/`. The parser is in `fbbot/scraper.py` (`extract_posts`). Add a captured sample to `tests/test_scraper.py` when you fix it.
- **Login or checkpoint.** The run stops, shows a macOS notification, and exits with code 2. Run `bot.py login` and clear the check in the window.
- **"comment box not found".** Your Facebook language may use a word that is not in the list. Add it to `post.comment_box_labels` in `config.yaml`.
- **launchd cannot read ~/Documents.** macOS may block background jobs in Documents. Either give `/bin/zsh` Full Disk Access (System Settings > Privacy & Security), move the folder, or use `bot.py loop`.
- **Run one command at a time.** Chrome locks the bot profile, so a second command fails while the first one is running.
- **Photos are not read.** Photo-only posts are skipped. Text with a photo is answered from the text alone.

## Links
- Code: `fbbot/` (`scraper.py` reads posts, `drafter.py` holds the Claude prompt, `poster.py` posts comments, `cli.py` holds the commands)
- Tone and facts: `knowledge.md`, `persona` in `config.yaml`
- Tests: `.venv/bin/python -m unittest`
