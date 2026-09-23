# Web3 Social + Content Intelligence Bot (V1)

**Standalone** — not Project Scout. You add projects yourself.

## Railway variables

### Required
```
TELEGRAM_BOT_TOKEN=...          # new bot from @BotFather (separate from Scout)
```

### Strongly recommended (AI content)
```
GROQ_API_KEY=...                # free — console.groq.com
# or
OPENROUTER_API_KEY=...          # free tier — openrouter.ai
```

### Optional
```
GEMINI_API_KEY=...
XAI_API_KEY=...
X_BEARER_TOKEN=...              # paid/limited — live X follower metrics
ALLOWED_USER_IDS=123456789       # lock bot to your Telegram user id(s)
DATABASE_PATH=/data/social.db   # use Railway volume path if available
```

## Deploy
1. Create a **new** Telegram bot with BotFather
2. New Railway service from this folder
3. Set env vars above
4. Start command: `python bot.py`

## V1 commands
- `/addproject` — guided or one-line
- `/projects` `/project <id>`
- `/social <id>` — dashboard (website + TG public + X if key)
- `/ideas` `/write` `/thread` `/rewrite` `/contentgaps`
- `/announce` `/communitycontent` `/daily` `/audit` `/voice`
- `/watch` `/watchlist`

## Stages
- **V1** (this): registry + public social fetch + AI content tools
- **V2**: historical metrics, content patterns, deeper audience
- **V3**: Scout import API, NL commands, competitor compare
