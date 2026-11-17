# FB Bot: Multi-Agent Travel Helper & Swarm

## Purpose
The bot automates engagement in Facebook travel and community groups from real Facebook accounts.

It can operate in two modes:
1. **Single-Agent Mode**: Reads posts in target groups, drafts helpful 1-2 sentence replies using local notes (`knowledge.md`), and posts comments after approval or auto-approval.
2. **Multi-Agent Swarm Mode (Coordinated Team)**: Multiple agents/accounts rotate primary answering and collaborate organically on posts without spamming:
   - **Primary Agent (Round-Robin Rotation)**: Picks up new traveler posts (unless comments already exist) and posts an authoritative, friendly answer.
   - **Secondary Agent(s)**: Inspects the post and the existing comment(s):
     - **BETTER COMMENT**: Only if the existing comment missed key info and the agent can write a clearly better standalone answer.
     - **LIKE + THREADED REPLY**: Likes the existing comment AND posts a short nested reply underneath with a complementary local tip after a natural delay (e.g. 15+ minutes).
     - **LIKE**: If the answer is already complete, likes the comment to boost its ranking and social proof.
     - **SKIP**: If the post is not relevant or already saturated, skips to prevent clutter.

---

## Architecture & Workflows

```mermaid
flowchart TD
    A["Post Scanned from Group Feed"] --> B{"Existing comment present?"}
    
    B -- "No (New Post)" --> C["Primary Agent (Round-Robin)\nDrafts helpful answer based on knowledge.md"]
    C --> D["Approved & Posted on Facebook"]
    
    B -- "Yes (Primary or FB Comment exists)" --> F["Next Agent AI Evaluation\n(Post + Existing comment + Replies)"]
    D --> F
    F --> G{"AI Collaboration Decision"}
    
    G -- "Can write clearly better answer" --> H1["Action: BETTER COMMENT\nPosts superior standalone comment"]
    G -- "Good answer + extra tip" --> H2["Action: LIKE + THREADED REPLY\nLikes comment & replies underneath"]
    G -- "Existing answer is complete" --> H3["Action: LIKE\nLikes existing comment only"]
    G -- "No value add needed" --> J["Action: SKIP\nIgnored to prevent spam"]
    
    H1 --> K["Execute after natural delay (anchored to live post time)"]
    H2 --> K
    H3 --> K
```

---

## Quick Setup

### 1. Setup Environment
```bash
# Activate virtual environment
.\.venv\Scripts\activate

# Install requirements
pip install -r requirements.txt
```

### 2. Configure Configuration & Secrets
1. Copy `config.example.yaml` to `config.yaml`:
   ```bash
   cp config.example.yaml config.yaml
   ```
2. Set your groups, destination, and define your agents:
   ```yaml
   groups:
     - https://www.facebook.com/groups/YOUR_GROUP_ID

   destination: "Lahore, Pakistan"

   agents:
     agent_1:
       name: "Ali (Food & Living)"
       persona: "a friendly local who knows street food and budget stays"
       browser_channel: chrome
       post:
         auto_post: false
         auto_post_min_confidence: high
         max_per_run: 5
         max_per_day: 15

     agent_2:
       name: "Hamza (Culture & Sights)"
       persona: "a local architecture enthusiast who loves historical sights"
       browser_channel: chrome
       post:
         auto_post: false
         auto_post_min_confidence: high
         max_per_run: 5
         max_per_day: 15
       collaboration:
         min_delay_after_primary_mins: 15
         allow_like: true
         allow_reply: true
         allow_like_and_reply: true
         allow_better_comment: true
   ```
3. Copy `.env.example` to `.env` and fill in your API key (`DASHSCOPE_API_KEY` for Qwen or `ANTHROPIC_API_KEY` for Claude).

---

## Daily Commands

### Login to Facebook Accounts
Each agent maintains its own isolated Chrome profile (`data/profiles/<agent_id>/`):
```bash
python bot.py login --agent agent_1   # Log in to Account 1
python bot.py login --agent agent_2   # Log in to Account 2
```

### Scan & Coordinate Actions
Scans target groups, rotates primary drafting across agents, and coordinates better comments / Likes+Replies:
```bash
python bot.py scan
```

### Review Queue
Interactively approve, edit, or skip pending comments, threaded replies, and likes:
```bash
python bot.py review
```

### Post Approved Actions
Executes comments, likes, and replies whose scheduled time has arrived:
```bash
python bot.py post               # Process due actions across all agents
python bot.py post --agent agent_1 # Process actions for specific agent
python bot.py post --dry-run     # Test without typing or clicking
```

### Run Full Cycle or Hourly Loop
```bash
python bot.py run                # 1 scan + post cycle
python bot.py loop --minutes 60  # Continuous hourly loop
# Windows Task Scheduler: scripts/hourly.ps1
# macOS launchd: scripts/hourly.sh + scripts/com.fbbot.hourly.plist
```

### Status & History
```bash
python bot.py status             # Activity per agent, ready actions, pending review
python bot.py history            # View recent comment logs
```

### Test AI Brain Offline
```bash
# Test primary reply:
python bot.py try "Coming to Lahore next week, where should I eat?" --agent agent_1

# Test collaboration on an existing comment:
python bot.py try "Coming to Lahore next week, where should I eat?" --agent agent_2 --existing-comment "Try Waris Nihari in Anarkali!" --primary-name "Ali"
```

---

## Safety & Anti-Ban Features
- **Isolated User Profiles**: Separate browser data and cookies for each agent account.
- **Natural Time Gaps**: Configurable delays (e.g. 15-45 minutes) between primary comment and secondary reactions.
- **No Direct Paste**: Uses human typing delays (35ms per keystroke).
- **Anti-Messenger Protection**: Automatically avoids Facebook chat and messenger dialogs.
- **Daily Caps**: Enforces strict `max_per_run` and `max_per_day` per account.
