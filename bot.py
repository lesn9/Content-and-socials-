"""
Web3 Social + Content Intelligence Bot (V1)
Standalone — NOT merged with Project Scout.

V1 scope (works day one):
  Project registry, social dashboard (best-effort free sources),
  content ideation / write / thread / rewrite / gaps,
  watchlist + simple alerts flag, AI via Groq/OpenRouter.

Later (V2/V3): historical metrics, deep X, Discord bot, Scout import API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from typing import Any

import aiosqlite
import httpx
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("social-content")

# ---------- env ----------

def env(*names: str, default: str = "") -> str:
    for n in names:
        v = (os.getenv(n) or "").strip().strip('"').strip("'")
        if v:
            return v
    return default


def env_present(*names: str) -> bool:
    return bool(env(*names))


def now() -> int:
    return int(time.time())


def esc(s: Any) -> str:
    t = str(s if s is not None else "")
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def ago(ts: int | None) -> str:
    if not ts:
        return "—"
    d = max(0, now() - int(ts))
    if d < 60:
        return f"{d}s ago"
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


HELP = """🐦 <b>Social + Content Intelligence</b>

Analyze any project from <b>one link</b> — no id required.

<b>Just pass what you have:</b>
/social https://x.com/handle
/social @handle
/social https://t.me/group
/social https://project.xyz
/social 0xContract…
/social solana:TokenMint…
/social base:0x…

Same pattern for:
/ideas · /write · /thread · /contentgaps · /daily · /audit · /announce · /communitycontent

<b>Optional save</b> (for watchlist / reuse):
/addproject — save a project you care about long-term
/projects — saved list
/watch · /watchlist

<b>V3</b> · personas · shuffle · cleaner briefs · link mining from X bio

<b>Intelligence</b>
/audience · /narratives · /topcontent · /contentpatterns
/mentions · /weekly · /calendar · /repurpose

<b>System</b>
/status · /testai · /help

Works with website, X, TG, or CA — any one is enough.
If AI fails: /testai shows the exact provider error.
X counts need a working bearer OR public mirrors; AI cannot invent live follower numbers.
How-to: developer.x.com → Project → App → Keys → Bearer Token (Read).
"""

# ---------- DB ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    ticker TEXT,
    chain TEXT,
    contract_address TEXT,
    website TEXT,
    x_handle TEXT,
    telegram TEXT,
    discord TEXT,
    github TEXT,
    docs TEXT,
    description TEXT,
    category TEXT,
    target_audience TEXT,
    voice_json TEXT,
    notes TEXT,
    status TEXT DEFAULT 'active',
    date_added INTEGER NOT NULL,
    last_social_at INTEGER,
    social_json TEXT
);
CREATE TABLE IF NOT EXISTS watches (
    user_id INTEGER NOT NULL,
    project_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, project_id)
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT,
    taken_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        self.c: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.c = await aiosqlite.connect(self.path)
        self.c.row_factory = aiosqlite.Row
        await self.c.executescript(SCHEMA)
        await self.c.commit()

    async def close(self) -> None:
        if self.c:
            await self.c.close()

    async def set_meta(self, k: str, v: str) -> None:
        await self.c.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (k, v),
        )
        await self.c.commit()

    async def get_meta(self, k: str) -> str | None:
        cur = await self.c.execute("SELECT value FROM meta WHERE key=?", (k,))
        row = await cur.fetchone()
        return row["value"] if row else None

    async def add_project(self, owner_id: int, data: dict[str, Any]) -> int:
        cur = await self.c.execute(
            """
            INSERT INTO projects(
                owner_id, name, ticker, chain, contract_address, website, x_handle,
                telegram, discord, github, docs, description, category,
                target_audience, voice_json, notes, status, date_added
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                owner_id,
                data.get("name") or "Unnamed",
                data.get("ticker"),
                data.get("chain"),
                data.get("contract_address"),
                data.get("website"),
                data.get("x_handle"),
                data.get("telegram"),
                data.get("discord"),
                data.get("github"),
                data.get("docs"),
                data.get("description"),
                data.get("category"),
                data.get("target_audience"),
                json.dumps(data.get("voice") or {}),
                data.get("notes"),
                "active",
                now(),
            ),
        )
        await self.c.commit()
        return int(cur.lastrowid)

    async def update_project(self, pid: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        await self.c.execute(f"UPDATE projects SET {cols} WHERE id=?", (*fields.values(), pid))
        await self.c.commit()

    async def by_id(self, pid: int) -> dict[str, Any] | None:
        cur = await self.c.execute("SELECT * FROM projects WHERE id=?", (pid,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def find(self, owner_id: int, query: str) -> dict[str, Any] | None:
        q = (query or "").strip()
        if not q:
            return None
        if q.isdigit():
            p = await self.by_id(int(q))
            if p and (not owner_id or p["owner_id"] == owner_id or True):
                return p
        cur = await self.c.execute(
            """
            SELECT * FROM projects
            WHERE owner_id=? AND (lower(name)=lower(?) OR lower(ticker)=lower(?)
                  OR lower(name) LIKE lower(?) OR id=?)
            ORDER BY date_added DESC LIMIT 1
            """,
            (owner_id, q, q, f"%{q}%", int(q) if q.isdigit() else -1),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_projects(self, owner_id: int, limit: int = 30) -> list[dict[str, Any]]:
        cur = await self.c.execute(
            "SELECT * FROM projects WHERE owner_id=? AND status!='removed' ORDER BY date_added DESC LIMIT ?",
            (owner_id, limit),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def remove(self, owner_id: int, pid: int) -> None:
        await self.c.execute(
            "UPDATE projects SET status='removed' WHERE id=? AND owner_id=?",
            (pid, owner_id),
        )
        await self.c.commit()

    async def watch(self, user_id: int, pid: int) -> None:
        await self.c.execute(
            "INSERT OR IGNORE INTO watches(user_id, project_id, created_at) VALUES(?,?,?)",
            (user_id, pid, now()),
        )
        await self.c.commit()

    async def unwatch(self, user_id: int, pid: int) -> None:
        await self.c.execute(
            "DELETE FROM watches WHERE user_id=? AND project_id=?",
            (user_id, pid),
        )
        await self.c.commit()

    async def watchlist(self, user_id: int) -> list[dict[str, Any]]:
        cur = await self.c.execute(
            """
            SELECT p.* FROM watches w
            JOIN projects p ON p.id = w.project_id
            WHERE w.user_id=? AND p.status!='removed'
            ORDER BY w.created_at DESC
            """,
            (user_id,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def save_snapshot(self, pid: int, kind: str, payload: dict[str, Any]) -> None:
        await self.c.execute(
            "INSERT INTO snapshots(project_id, kind, payload, taken_at) VALUES(?,?,?,?)",
            (pid, kind, json.dumps(payload), now()),
        )
        await self.c.commit()


# ---------- AI ----------

KEY_STATUS: dict[str, str] = {}

PERSONAS = {
    "founder": "Founder voice: direct, first-person, product-focused, short sentences, minimal hype.",
    "community": "Community manager: warm, inclusive, asks questions, lightly playful, no corporate fluff.",
    "educator": "Educator: clear analogies, step-by-step, patient, zero jargon unless explained.",
    "analyst": "Market-aware analyst: precise, cautious claims, structured, no shill language.",
    "degen": "Crypto-native degen: punchy, meme-literate, still accurate — not fake slang spam.",
    "minimal": "Minimalist: fewest words possible, high signal, no emojis unless necessary.",
}


SEP = "━━━━━━━━━━━━━━━━━━━━━━"


def dash_header(title: str, project: str) -> str:
    return f"{title}\n\nProject: <b>{esc(project)}</b>\n\n{SEP}"


def dash_line(label: str, value: Any) -> str:
    v = value if value is not None and str(value).strip() and str(value) != "None" else "—"
    return f"{label}: {esc(v)}"


def dash_bullets(items: list[str], limit: int = 8) -> str:
    out = []
    for it in items[:limit]:
        it = (it or "").strip()
        if it:
            out.append(f"• {esc(it)}")
    return "\n".join(out) if out else "• —"


def dash_questions(items: list[str], limit: int = 6) -> str:
    out = []
    for it in items[:limit]:
        it = (it or "").strip().lstrip("?").strip()
        if it:
            out.append(f"❓ {esc(it)}")
    return "\n".join(out) if out else "❓ —"


def strip_md(text: str) -> str:
    t = text or ""
    t = t.replace("**", "").replace("__", "")
    t = re.sub(r"(?m)^\s*#{1,6}\s*", "", t)
    t = re.sub(r"`+", "", t)
    return t.strip()


def parse_labeled_sections(raw: str, labels: list[str]) -> dict[str, str]:
    """Split AI text by known ALL-CAPS or Title labels into buckets."""
    raw = strip_md(raw)
    if not raw:
        return {lab: "" for lab in labels}
    # Build regex alternation
    alt = "|".join(re.escape(l) for l in labels)
    parts = re.split(rf"(?im)^\s*(?:{alt})\s*:?\s*$", raw)
    # re.split with capturing would be better
    parts2 = re.split(rf"(?im)^\s*({alt})\s*:?\s*$", raw)
    buckets = {lab.upper(): "" for lab in labels}
    # parts2: [pre, LABEL, body, LABEL, body, ...]
    i = 1
    while i + 1 < len(parts2):
        lab = parts2[i].strip().upper()
        body = parts2[i + 1].strip()
        # normalize label keys
        for L in labels:
            if L.upper() == lab:
                buckets[L.upper()] = body
                break
        i += 2
    if not any(buckets.values()) and raw:
        buckets[labels[0].upper()] = raw
    return buckets


def bullets_from_block(block: str, limit: int = 6) -> list[str]:
    items = []
    for ln in (block or "").splitlines():
        ln = ln.strip().lstrip("•-–— ").strip()
        if ln:
            items.append(ln)
    if not items and block:
        # split sentences lightly
        for bit in re.split(r"(?<=[.!?])\s+", block.strip()):
            if bit.strip():
                items.append(bit.strip())
    return items[:limit]


def copyable(text: str) -> str:
    return f"<code>{esc(strip_md(text))}</code>" if text else "<code>—</code>"


def user_limitations(social: dict[str, Any], ai_ok: bool) -> list[str]:
    lim = []
    x = social.get("x") or {}
    tg = social.get("telegram") or {}
    web = social.get("website") or {}
    if x.get("followers") is None and x.get("following") is None:
        lim.append("Live X metrics unavailable.")
    if tg.get("members") is None and not (tg.get("title") or tg.get("about")):
        lim.append("Telegram public data limited.")
    if not (web.get("ok") or web.get("title") or web.get("about")):
        lim.append("Website details limited.")
    if not ai_ok:
        lim.append("AI analysis unavailable.")
    return lim




def clean_ai_html(text: str) -> str:
    """Turn messy markdown-ish AI output into clean Telegram HTML blocks."""
    if not text:
        return ""
    t = text.strip()
    # strip bold markdown leftovers carefully
    t = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"__([^_]+)__", r"<b>\1</b>", t)
    t = re.sub(r"(?m)^\s*#{1,3}\s*", "", t)
    t = re.sub(r"(?m)^\s*[-•]\s+", "• ", t)
    # collapse excess blank lines
    t = re.sub(r"\n{3,}", "\n\n", t)
    return esc(t) if "<b>" not in t else t  # if we added tags, don't double-esc whole thing


def format_section(title: str, body: str) -> str:
    body = (body or "").strip()
    if not body:
        return f"<b>{esc(title)}</b>\n—"
    # if body already has html from clean, use as-is after light pass
    lines = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            lines.append("")
            continue
        if line.startswith("•") or line.startswith("-"):
            lines.append("• " + esc(line.lstrip("•- ").strip()))
        elif line.isupper() and len(line) < 40:
            lines.append(f"<b>{esc(line.title())}</b>")
        else:
            lines.append(esc(line))
    return f"<b>{esc(title)}</b>\n" + "\n".join(lines)


def extract_links_from_text(blob: str) -> dict[str, str | None]:
    """Pull website / telegram / discord from bio or page text."""
    out: dict[str, str | None] = {"website": None, "telegram": None, "discord": None}
    if not blob:
        return out
    # t.me
    m = re.search(r"https?://t\.me/[A-Za-z0-9_]+", blob)
    if m:
        out["telegram"] = m.group(0)
    # discord
    m = re.search(r"https?://(discord\.gg|discord\.com/invite)/[A-Za-z0-9-]+", blob, re.I)
    if m:
        out["discord"] = m.group(0)
    # generic urls skip x.com twitter t.co for website preference
    for m in re.finditer(r"https?://[^\s<>\"']+", blob):
        u = m.group(0).rstrip(").,]")
        low = u.lower()
        if any(x in low for x in ("x.com/", "twitter.com/", "t.me/", "discord.", "t.co/")):
            continue
        if not out["website"]:
            out["website"] = u
    return out


def project_nav_keyboard(pid: int, extra: list[list] | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = extra[:] if extra else []
    rows.append(
        [
            InlineKeyboardButton("🐦 Social", callback_data=f"soc:{pid}"),
            InlineKeyboardButton("💡 Ideas", callback_data=f"id:{pid}"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("✍️ Write", callback_data=f"wr:{pid}"),
            InlineKeyboardButton("🔀 Shuffle write", callback_data=f"shw:{pid}"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("🧩 Gaps", callback_data=f"gap:{pid}"),
            InlineKeyboardButton("📅 Calendar", callback_data=f"cal:{pid}"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("👤 Personas", callback_data=f"per:{pid}"),
            InlineKeyboardButton("⭐ Watch", callback_data=f"wa:{pid}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def persona_keyboard(pid: int) -> InlineKeyboardMarkup:
    rows = []
    keys = list(PERSONAS.keys())
    for i in range(0, len(keys), 2):
        chunk = keys[i : i + 2]
        rows.append(
            [
                InlineKeyboardButton(k.title(), callback_data=f"pw:{pid}:{k}")
                for k in chunk
            ]
        )
    rows.append([InlineKeyboardButton("« Back", callback_data=f"p:{pid}")])
    return InlineKeyboardMarkup(rows)




async def llm_write(prompt: str, system: str | None = None) -> str:
    """Free-first: Groq → OpenRouter → Gemini → xAI/OpenAI. Stores errors in KEY_STATUS."""
    sys_msg = system or (
        "You are a Web3 social & content strategist. Be specific to the project. "
        "Never invent product facts not given in the prompt. No investment advice. "
        "Plain text, scannable bullets when useful."
    )
    # Support common Railway naming mistakes
    groq = env("GROQ_API_KEY", "GROQ_KEY", "GROQ")
    ork = env("OPENROUTER_API_KEY", "OPENROUTER_KEY", "OPEN_ROUTER_API_KEY")
    gem = env("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_AI_API_KEY")
    xai = env("XAI_API_KEY", "GROK_API_KEY")
    oai = env("OPENAI_API_KEY")

    attempts: list[tuple[str, str, str, str]] = []
    # name, key, url, model
    if groq:
        for model in (
            "llama-3.3-70b-versatile",
            "openai/gpt-oss-20b",
            "openai/gpt-oss-120b",
            "llama-3.1-8b-instant",
        ):
            attempts.append(("groq", groq, "https://api.groq.com/openai/v1/chat/completions", model))
    if ork:
        for model in (
            "meta-llama/llama-3.3-70b-instruct:free",
            "google/gemini-2.0-flash-exp:free",
            "openai/gpt-4o-mini",
            "meta-llama/llama-3.1-8b-instruct:free",
        ):
            attempts.append(
                ("openrouter", ork, "https://openrouter.ai/api/v1/chat/completions", model)
            )

    async with httpx.AsyncClient(timeout=75) as client:
        for name, key, url, model in attempts:
            try:
                headers = {
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                }
                if name == "openrouter":
                    headers["HTTP-Referer"] = "https://railway.app"
                    headers["X-Title"] = "SocialContentBot"
                r = await client.post(
                    url,
                    headers=headers,
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": sys_msg},
                            {"role": "user", "content": prompt[:12000]},
                        ],
                        "temperature": 0.7,
                    },
                )
                body_snip = (r.text or "")[:180].replace("\n", " ")
                if r.status_code >= 400:
                    KEY_STATUS[name] = f"http {r.status_code}: {body_snip}"
                    log.warning("%s %s -> %s %s", name, model, r.status_code, body_snip)
                    continue
                data = r.json()
                text = (
                    (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
                ).strip()
                if text:
                    KEY_STATUS[name] = f"ok:{model}"
                    return text
                KEY_STATUS[name] = f"empty response:{model}"
            except Exception as exc:
                KEY_STATUS[name] = str(exc)[:120]
                log.warning("%s failed: %s", name, exp if False else exc)

        if gem:
            for model in ("gemini-2.0-flash", "gemini-1.5-flash", "gemini-2.5-flash"):
                try:
                    r = await client.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                        params={"key": gem},
                        json={
                            "contents": [
                                {"parts": [{"text": (sys_msg + "\n\n" + prompt)[:14000]}]}
                            ]
                        },
                    )
                    if r.status_code >= 400:
                        KEY_STATUS["gemini"] = f"http {r.status_code}: {(r.text or '')[:120]}"
                        continue
                    data = r.json()
                    parts = (
                        ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
                    )
                    text = "\n".join(p.get("text") or "" for p in parts).strip()
                    if text:
                        KEY_STATUS["gemini"] = f"ok:{model}"
                        return text
                except Exception as exc:
                    KEY_STATUS["gemini"] = str(exc)[:120]

        if xai or oai:
            key = xai or oai
            base = (
                "https://api.x.ai/v1/chat/completions"
                if xai
                else "https://api.openai.com/v1/chat/completions"
            )
            model = "grok-2-latest" if xai else "gpt-4o-mini"
            tag = "xai" if xai else "openai"
            try:
                r = await client.post(
                    base,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": sys_msg},
                            {"role": "user", "content": prompt[:12000]},
                        ],
                    },
                )
                if r.status_code < 400:
                    data = r.json()
                    text = (
                        (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
                    ).strip()
                    if text:
                        KEY_STATUS[tag] = f"ok:{model}"
                        return text
                KEY_STATUS[tag] = f"http {r.status_code}: {(r.text or '')[:120]}"
            except Exception as exc:
                KEY_STATUS[tag] = str(exc)[:120]

    if not any([groq, ork, gem, xai, oai]):
        KEY_STATUS["env"] = "NO_KEYS_VISIBLE — check this service Variables, not Scout"
    return ""


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Developer diagnostics — not shown inside intelligence reports."""
    await cmd_status(update, context)


async def cmd_testai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force-test AI keys and show exact error."""
    if not await gate(update, context) or not update.effective_message:
        return
    await update.effective_message.reply_text("Testing AI providers…")
    text = await llm_write("Reply with exactly: AI_OK and one short sentence about Web3 content.")
    g = env("GROQ_API_KEY", "GROQ_KEY", "GROQ")
    o = env("OPENROUTER_API_KEY", "OPENROUTER_KEY", "OPEN_ROUTER_API_KEY")
    lines = [
        "🧪 <b>AI TEST</b>",
        f"GROQ_API_KEY visible: <b>{'YES' if g else 'NO'}</b> (len={len(g)})",
        f"OPENROUTER_API_KEY visible: <b>{'YES' if o else 'NO'}</b> (len={len(o)})",
        f"Gemini: {'YES' if env('GEMINI_API_KEY') else 'NO'}",
        "",
        "Provider status:",
    ]
    for k, v in KEY_STATUS.items():
        lines.append(f"• {esc(k)}: {esc(v)}")
    if text:
        lines += ["", "✅ Model reply:", esc(text[:500])]
    else:
        lines += [
            "",
            "❌ No provider returned text.",
            "Same Railway <b>project</b> is fine — keys must be on <b>this service</b> Variables.",
            "After adding vars: Redeploy / Restart the Social service.",
        ]
    await update.effective_message.reply_html("\n".join(lines))


# ---------- fetch helpers ----------

async def http_get(client: httpx.AsyncClient, url: str, **kw: Any) -> Any:
    try:
        r = await client.get(url, timeout=20, follow_redirects=True, **kw)
        if r.status_code >= 400:
            return None
        ct = r.headers.get("content-type", "")
        if "json" in ct:
            return r.json()
        return r.text
    except Exception as exc:
        log.warning("get %s: %s", url, exc)
        return None


def normalize_x(handle: str | None) -> str | None:
    if not handle:
        return None
    h = handle.strip()
    h = re.sub(r"^https?://(www\.)?(twitter|x)\.com/", "", h, flags=re.I)
    h = h.strip("/").lstrip("@")
    return h or None


def normalize_tg(url: str | None) -> str | None:
    if not url:
        return None
    u = url.strip()
    if u.startswith("@"):
        return f"https://t.me/{u[1:]}"
    if "t.me/" in u and not u.startswith("http"):
        return "https://" + u
    return u



def looks_like_ca(s: str) -> bool:
    s = s.strip()
    if s.startswith("0x") and len(s) >= 40:
        return True
    if ":" in s and not s.startswith("http"):
        return True
    # solana-ish base58 length
    if 32 <= len(s) <= 50 and s.isalnum():
        return True
    return False


def looks_like_x(s: str) -> bool:
    s = s.strip()
    return bool(
        s.startswith("@")
        or "x.com/" in s.lower()
        or "twitter.com/" in s.lower()
    )


def looks_like_tg(s: str) -> bool:
    return "t.me/" in s.lower() or s.strip().startswith("@") and False  # @ is X default


def looks_like_url(s: str) -> bool:
    return s.strip().startswith("http://") or s.strip().startswith("https://")


async def resolve_or_create_from_query(
    db: DB, client: httpx.AsyncClient, owner_id: int, query: str
) -> dict[str, Any] | None:
    """Accept website / X / TG / CA — find saved project or create a working draft."""
    q = (query or "").strip()
    if not q:
        return None

    # numeric id still works for saved projects
    if q.isdigit():
        p = await db.by_id(int(q))
        if p:
            return p

    # try name match on saved
    found = await db.find(owner_id, q)
    if found and not looks_like_url(q) and not looks_like_x(q) and not looks_like_ca(q):
        return found

    data: dict[str, Any] = {"name": "Untitled project"}
    chain = None
    addr = None

    if looks_like_x(q):
        h = normalize_x(q)
        data["x_handle"] = h
        data["name"] = f"@{h}" if h else "X project"
    elif "t.me/" in q.lower():
        data["telegram"] = normalize_tg(q)
        slug = q.rstrip("/").split("/")[-1]
        data["name"] = f"TG:{slug}"
    elif looks_like_url(q):
        data["website"] = q
        # rough name from host
        host = q.split("//")[-1].split("/")[0].replace("www.", "")
        data["name"] = host
    elif looks_like_ca(q):
        if ":" in q:
            chain, addr = q.split(":", 1)
            chain, addr = chain.lower().strip(), addr.strip()
        else:
            addr = q
            chain = "ethereum" if addr.startswith("0x") else "solana"
        data["chain"] = chain
        data["contract_address"] = addr
        data["name"] = f"{chain}:{addr[:8]}…"
        # try DexScreener for name + socials
        try:
            if chain and addr:
                pairs = await http_get(client, f"https://api.dexscreener.com/tokens/v1/{chain}/{addr}")
                if isinstance(pairs, list) and pairs:
                    pair = pairs[0]
                    base = pair.get("baseToken") or {}
                    data["name"] = base.get("name") or data["name"]
                    data["ticker"] = base.get("symbol")
                    info = pair.get("info") or {}
                    for link in info.get("socials") or []:
                        if not isinstance(link, dict):
                            continue
                        u = (link.get("url") or "").strip()
                        t = (link.get("type") or "").lower()
                        if "twitter" in t or "x.com" in u:
                            data["x_handle"] = normalize_x(u)
                        elif "telegram" in t or "t.me" in u:
                            data["telegram"] = normalize_tg(u)
                    for link in info.get("websites") or []:
                        if isinstance(link, dict) and link.get("url"):
                            data["website"] = link["url"]
                            break
                    if pair.get("url") and not data.get("website"):
                        pass
        except Exception as exc:
            log.warning("dex resolve: %s", exp if False else exc)
    else:
        # treat as name search only
        if found:
            return found
        data["name"] = q

    # match existing by x / website / tg / ca
    rows = await db.list_projects(owner_id, limit=100)
    for p in rows:
        if data.get("x_handle") and normalize_x(p.get("x_handle")) == normalize_x(data.get("x_handle")):
            return p
        if data.get("website") and (p.get("website") or "").rstrip("/") == data["website"].rstrip("/"):
            return p
        if data.get("telegram") and (p.get("telegram") or "") == data["telegram"]:
            return p
        if data.get("contract_address") and (p.get("contract_address") or "").lower() == data["contract_address"].lower():
            if not data.get("chain") or (p.get("chain") or "").lower() == (data.get("chain") or "").lower():
                return p

    pid = await db.add_project(owner_id, data)
    social = await gather_social(client, data)
    about = (social.get("website") or {}).get("about")
    updates = {"social_json": __import__("json").dumps(social), "last_social_at": now()}
    if about:
        updates["description"] = about
    # improve name from site title
    title = (social.get("website") or {}).get("title") or (social.get("telegram") or {}).get("title")
    if title:
        weak = (
            data["name"].startswith("http")
            or data["name"].startswith("@")
            or data["name"].startswith("TG:")
            or data["name"].startswith("Untitled")
            or "…" in data["name"]
        )
        if weak:
            updates["name"] = title[:80]
    await db.update_project(pid, **updates)
    return await db.by_id(pid)


async def fetch_website_brief(client: httpx.AsyncClient, url: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {"url": url, "title": None, "about": None, "ok": False}
    if not url or not url.startswith("http"):
        return out
    html = await http_get(client, url, headers={"User-Agent": "Mozilla/5.0 SocialContentBot/1.0"})
    if not isinstance(html, str):
        return out
    try:
        soup = BeautifulSoup(html, "lxml")
        title = (soup.title.string if soup.title else None) or ""
        desc = ""
        md = soup.find("meta", attrs={"name": "description"}) or soup.find(
            "meta", attrs={"property": "og:description"}
        )
        if md and md.get("content"):
            desc = md["content"]
        if not desc:
            p = soup.find("p")
            if p:
                desc = p.get_text(" ", strip=True)[:400]
        out["title"] = title.strip()[:200]
        out["about"] = desc.strip()[:500]
        out["ok"] = True
    except Exception as exc:
        log.warning("website parse: %s", exc)
    return out


async def fetch_tg_brief(client: httpx.AsyncClient, url: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {"url": url, "title": None, "about": None, "members": None, "ok": False}
    if not url or "t.me/" not in url:
        return out
    html = await http_get(client, url, headers={"User-Agent": "Mozilla/5.0"})
    if not isinstance(html, str):
        return out
    try:
        soup = BeautifulSoup(html, "lxml")
        title = soup.find("div", class_="tgme_page_title")
        desc = soup.find("div", class_="tgme_page_description")
        extra = soup.find("div", class_="tgme_page_extra")
        out["title"] = title.get_text(" ", strip=True) if title else None
        out["about"] = desc.get_text(" ", strip=True) if desc else None
        if extra:
            m = re.search(r"([\d\s]+)\s*members", extra.get_text(" ", strip=True), re.I)
            if m:
                out["members"] = int(re.sub(r"\s+", "", m.group(1)))
        out["ok"] = bool(out["title"] or out["about"])
    except Exception as exc:
        log.warning("tg parse: %s", exp if (exp := exc) else exc)
    return out


async def fetch_x_brief(client: httpx.AsyncClient, handle: str | None) -> dict[str, Any]:
    """Official API if bearer set; else best-effort public page scrape."""
    out: dict[str, Any] = {
        "handle": handle,
        "followers": None,
        "following": None,
        "posts": None,
        "bio": None,
        "ok": False,
        "note": None,
        "recent": [],
    }
    h = normalize_x(handle)
    if not h:
        return out
    out["handle"] = h
    bearer = env("X_BEARER_TOKEN")
    if bearer:
        headers = {"Authorization": f"Bearer {bearer}"}
        last_err = None
        for base in (
            "https://api.x.com/2",
            "https://api.twitter.com/2",
        ):
            try:
                r = await client.get(
                    f"{base}/users/by/username/{h}",
                    params={"user.fields": "public_metrics,description,url,entities"},
                    headers=headers,
                    timeout=20,
                )
                if r.status_code >= 400:
                    last_err = f"http {r.status_code}: {(r.text or '')[:100]}"
                    continue
                data = (r.json().get("data") or {})
                metrics = data.get("public_metrics") or {}
                out["followers"] = metrics.get("followers_count")
                out["following"] = metrics.get("following_count")
                out["posts"] = metrics.get("tweet_count")
                out["bio"] = data.get("description")
                # entities.url may hold expanded website
                ents = data.get("entities") or {}
                url_ents = (ents.get("url") or {}).get("urls") or []
                desc_ents = (ents.get("description") or {}).get("urls") or []
                for u in url_ents + desc_ents:
                    exp = u.get("expanded_url") or u.get("url")
                    if exp:
                        out["bio"] = (out.get("bio") or "") + " " + exp
                out["ok"] = True
                return out
            except Exception as e:
                last_err = str(e)[:100]
        out["note"] = last_err or "X API failed"

        # Public fallbacks (no official API) — best-effort only
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/json",
    }
    pages = [
        f"https://x.com/{h}",
        f"https://nitter.net/{h}",
        f"https://nitter.privacydev.net/{h}",
    ]
    for url in pages:
        try:
            html = await http_get(client, url, headers=headers)
            if not isinstance(html, str) or len(html) < 200:
                continue
            # bio / og description
            m = re.search(
                r'property="og:description"\s+content="([^"]+)"', html, re.I
            ) or re.search(
                r'content="([^"]+)"\s+property="og:description"', html, re.I
            )
            if m and not out.get("bio"):
                out["bio"] = html_unescape(m.group(1))[:400]
            # nitter profile stats often as plain text
            m_f = re.search(
                r'([\d,\.]+[KMB]?)\s*Followers', html, re.I
            ) or re.search(
                r'followers["\s:]+([\d,]+)', html, re.I
            )
            if m_f and out.get("followers") is None:
                out["followers"] = parse_count(m_f.group(1))
            m_g = re.search(
                r'([\d,\.]+[KMB]?)\s*Following', html, re.I
            )
            if m_g and out.get("following") is None:
                out["following"] = parse_count(m_g.group(1))
            m_p = re.search(
                r'([\d,\.]+[KMB]?)\s*Posts', html, re.I
            ) or re.search(
                r'([\d,\.]+[KMB]?)\s*Tweets', html, re.I
            )
            if m_p and out.get("posts") is None:
                out["posts"] = parse_count(m_p.group(1))
            if out.get("bio") or out.get("followers") is not None:
                out["ok"] = True
                out["note"] = f"Public source: {url.split('/')[2]}"
                break
        except Exception as e:
            out["note"] = f"scrape: {str(e)[:80]}"
    if not out.get("ok") and not out.get("note"):
        out["note"] = "No live X metrics — API token limited; public mirrors also blocked"
    return out


def html_unescape(s: str) -> str:
    import html as _html
    return _html.unescape(s or "")


def copy_block(text: str) -> str:
    """Telegram-copyable monospace block."""
    t = (text or "").strip()
    if not t:
        return "<code>—</code>"
    return f"<code>{esc(t)}</code>"


def format_ideas_clean(raw: str) -> str:
    """6 ideas → numbered clean copyable cards."""
    if not raw:
        return "—"
    t = raw.replace("**", "").replace("__", "")
    t = re.sub(r"(?m)^\s*#{1,3}\s*", "", t)
    # split on numbered items
    parts = re.split(r"(?m)^\s*(?:\d+[.)]|#{1,3}\s*\d+[.)]?)\s*", t)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) <= 1:
        return copy_block(t[:3500])
    out = []
    for i, p in enumerate(parts[:8], 1):
        lines = [ln.strip() for ln in p.splitlines() if ln.strip()]
        title = lines[0] if lines else f"Idea {i}"
        title = re.sub(r"^[Tt]itle:\s*", "", title)
        title = title.strip('"')
        rest = "\n".join(lines[1:]) if len(lines) > 1 else ""
        out.append(f"<b>{i}.</b> {esc(title)}")
        if rest:
            out.append(copy_block(rest[:800]))
        out.append("")
    return "\n".join(out)


def parse_count(raw: str) -> int | None:
    if not raw:
        return None
    t = str(raw).strip().upper().replace(",", "")
    mult = 1
    if t.endswith("K"):
        mult = 1_000
        t = t[:-1]
    elif t.endswith("M"):
        mult = 1_000_000
        t = t[:-1]
    elif t.endswith("B"):
        mult = 1_000_000_000
        t = t[:-1]
    try:
        return int(float(t) * mult)
    except ValueError:
        return None


async def gather_social(client: httpx.AsyncClient, p: dict[str, Any]) -> dict[str, Any]:
    """Pull X first, mine bio for TG/website, then fetch those pages."""
    x = await fetch_x_brief(client, p.get("x_handle") or p.get("twitter"))
    website_url = p.get("website")
    tg_url = normalize_tg(p.get("telegram"))
    # Mine bio + note for links
    blob = " ".join(
        str(x.get(k) or "") for k in ("bio", "note")
    )
    found = extract_links_from_text(blob)
    if not website_url and found.get("website"):
        website_url = found["website"]
    if not tg_url and found.get("telegram"):
        tg_url = found["telegram"]
    website = await fetch_website_brief(client, website_url)
    # Site may also list telegram
    site_blob = " ".join(
        str((website or {}).get(k) or "") for k in ("about", "title", "url")
    )
    found2 = extract_links_from_text(site_blob + " " + str((website or {}).get("about") or ""))
    if not tg_url and found2.get("telegram"):
        tg_url = found2["telegram"]
    tg = await fetch_tg_brief(client, tg_url)
    return {
        "website": website,
        "telegram": tg,
        "x": x,
        "discovered_links": {
            "website": website_url,
            "telegram": tg_url,
            "discord": found.get("discord") or found2.get("discord"),
        },
        "gathered_at": now(),
    }


def project_context(p: dict[str, Any], social: dict[str, Any] | None = None) -> str:
    lines = [
        f"Name: {p.get('name')}",
        f"Ticker: {p.get('ticker') or '—'}",
        f"Chain: {p.get('chain') or '—'}",
        f"Contract: {p.get('contract_address') or '—'}",
        f"Website: {p.get('website') or '—'}",
        f"X: {p.get('x_handle') or '—'}",
        f"Telegram: {p.get('telegram') or '—'}",
        f"Discord: {p.get('discord') or '—'}",
        f"Docs: {p.get('docs') or '—'}",
        f"Category: {p.get('category') or '—'}",
        f"Audience: {p.get('target_audience') or '—'}",
        f"Description: {p.get('description') or '—'}",
        f"Notes: {p.get('notes') or '—'}",
    ]
    if social:
        w = social.get("website") or {}
        t = social.get("telegram") or {}
        x = social.get("x") or {}
        lines += [
            f"Site title: {w.get('title') or '—'}",
            f"Site about: {w.get('about') or '—'}",
            f"TG title: {t.get('title') or '—'} members={t.get('members')}",
            f"TG about: {t.get('about') or '—'}",
            f"X followers: {x.get('followers')} bio: {x.get('bio') or x.get('note') or '—'}",
        ]
    voice = {}
    try:
        voice = json.loads(p.get("voice_json") or "{}")
    except Exception:
        pass
    if voice:
        lines.append(f"Voice profile: {json.dumps(voice)}")
    return "\n".join(lines)


# ---------- auth ----------

def allowed(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    owners: list[int] = context.application.bot_data.get("allowed") or []
    if not owners:
        return True
    return user_id in owners


async def claim_owner(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    configured = context.application.bot_data.get("allowed") or []
    if configured:
        return
    db: DB = context.application.bot_data["db"]
    existing = await db.get_meta("owner_id")
    if existing:
        context.application.bot_data["allowed"] = [int(existing)]
        return
    await db.set_meta("owner_id", str(user_id))
    context.application.bot_data["allowed"] = [user_id]
    log.info("Owner locked to %s", user_id)


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    await claim_owner(context, user.id)
    if not allowed(user.id, context):
        if update.effective_message:
            await update.effective_message.reply_text("This bot is private.")
        return False
    return True


def deps(context: ContextTypes.DEFAULT_TYPE) -> tuple[DB, httpx.AsyncClient]:
    return context.application.bot_data["db"], context.application.bot_data["http"]


async def resolve_project(update: Update, context: ContextTypes.DEFAULT_TYPE, arg: str | None) -> dict[str, Any] | None:
    if not arg:
        return None
    db, client = deps(context)
    uid = update.effective_user.id if update.effective_user else 0
    return await resolve_or_create_from_query(db, client, uid, arg)


# ---------- handlers ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_message:
        return
    await claim_owner(context, update.effective_user.id)
    if not await gate(update, context):
        return
    await update.effective_message.reply_html(
        "🐦 <b>Social + Content Intelligence online</b>\n\n"
        "Paste a link or CA — no id needed:\n"
        "/social https://x.com/handle\n"
        "/social https://t.me/group\n"
        "/social https://project.site\n"
        "/social base:0x…\n\n"
        "/ideas · /write · /contentgaps work the same way.\n"
        "/addproject is optional (save for watchlist).\n"
        "/status — check AI keys"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    await update.effective_message.reply_html(HELP, disable_web_page_preview=True)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    g = env("GROQ_API_KEY")
    o = env("OPENROUTER_API_KEY")
    lines = [
        "📡 <b>Status</b>",
        f"Groq key in env: <b>{'YES' if g else 'NO'}</b> · last try: {esc(KEY_STATUS.get('groq') or 'not tried yet')}",
        f"OpenRouter key in env: <b>{'YES' if o else 'NO'}</b> · last try: {esc(KEY_STATUS.get('openrouter') or 'not tried yet')}",
        f"Gemini: {'YES' if env('GEMINI_API_KEY') else 'NO'} · {esc(KEY_STATUS.get('gemini') or '—')}",
        f"X bearer: {'YES' if env('X_BEARER_TOKEN') else 'NO'}",
        f"DB: {esc(os.getenv('DATABASE_PATH', './social.db'))}",
        "",
        "If keys say NO: put them on <b>this</b> Railway service (Social bot), not Scout, then redeploy.",
        "If YES but AI offline: open /ideas once to force a try, then /status again.",
    ]
    await update.effective_message.reply_html("\n".join(lines))


def parse_add_line(text: str) -> dict[str, Any]:
    """Name | @x | website | telegram | chain | contract"""
    parts = [p.strip() for p in text.split("|")]
    data: dict[str, Any] = {}
    if parts:
        data["name"] = parts[0]
    if len(parts) > 1 and parts[1]:
        data["x_handle"] = normalize_x(parts[1])
    if len(parts) > 2 and parts[2]:
        data["website"] = parts[2]
    if len(parts) > 3 and parts[3]:
        data["telegram"] = normalize_tg(parts[3])
    if len(parts) > 4 and parts[4]:
        data["chain"] = parts[4].lower()
    if len(parts) > 5 and parts[5]:
        data["contract_address"] = parts[5]
    return data


async def cmd_addproject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message or not update.effective_user:
        return
    args = context.args or []
    db, client = deps(context)
    if args:
        raw = " ".join(args)
        if "|" in raw:
            data = parse_add_line(raw)
        else:
            # /addproject Name @handle https://site
            data = {"name": args[0]}
            for a in args[1:]:
                if a.startswith("@") or "x.com/" in a or "twitter.com/" in a:
                    data["x_handle"] = normalize_x(a)
                elif "t.me/" in a:
                    data["telegram"] = normalize_tg(a)
                elif a.startswith("http"):
                    data["website"] = a
                elif a.startswith("0x") or len(a) > 30:
                    data["contract_address"] = a
                else:
                    data["chain"] = a.lower()
        if not data.get("name"):
            await update.effective_message.reply_text("Need at least a project name.")
            return
        pid = await db.add_project(update.effective_user.id, data)
        # light enrich description from website
        social = await gather_social(client, data)
        about = (social.get("website") or {}).get("about")
        if about:
            await db.update_project(pid, description=about, social_json=json.dumps(social), last_social_at=now())
        await update.effective_message.reply_html(
            f"✅ Added <b>#{pid} {esc(data['name'])}</b>\n"
            f"X: {esc(data.get('x_handle') or '—')} · Web: {esc(data.get('website') or '—')}\n"
            f"TG: {esc(data.get('telegram') or '—')}\n\n"
            f"Next: /social {pid} · /ideas {pid} · /write {pid}"
        )
        return
    context.user_data["add_flow"] = {"step": "name", "data": {}}
    await update.effective_message.reply_text(
        "Optional: save a project for /watchlist.\n"
        "Or skip this — just run /social with a link.\n\n"
        "Guided: send the project name (or /cancel).\n"
        "One-shot:\n"
        "/addproject Name | @x | https://site | https://t.me/… | chain | CA"
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    context.user_data.pop("add_flow", None)
    context.user_data.pop("rewrite_pid", None)
    await update.effective_message.reply_text("Cancelled.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_user:
        return
    if not await gate(update, context):
        return
    text = (update.effective_message.text or "").strip()
    if not text or text.startswith("/"):
        return

    # repurpose paste flow
    if context.user_data.get("repurpose_pid"):
        pid = int(context.user_data.pop("repurpose_pid"))
        db, client = deps(context)
        p = await db.by_id(pid)
        if not p:
            await update.effective_message.reply_text("Project gone.")
            return
        social = await gather_social(client, p)
        prompt = (
            project_context(p, social)
            + "\n\nSOURCE CONTENT:\n"
            + text
            + "\n\nRepurpose into: 1) X post 2) X thread outline 3) Telegram post "
            + "4) Discord note 5) FAQ bullets 6) 3 follow-up content ideas."
        )
        await update.effective_message.reply_text("Repurposing...")
        out = await llm_write(prompt)
        await update.effective_message.reply_html(
            "♻️ <b>REPURPOSE · "
            + esc(p["name"])
            + "</b>\n\n"
            + esc(out or _ai_fail())
        )
        return

    # rewrite paste flow
    if context.user_data.get("rewrite_pid"):
        pid = int(context.user_data.pop("rewrite_pid"))
        db, client = deps(context)
        p = await db.by_id(pid)
        if not p:
            await update.effective_message.reply_text("Project gone.")
            return
        social = await gather_social(client, p)
        prompt = (
            f"{project_context(p, social)}\n\n"
            f"Rewrite the following content to be clearer, stronger hook, still accurate:\n\n{text}\n\n"
            f"Provide: 1) shorter version 2) X-friendly version 3) Telegram-friendly version."
        )
        await update.effective_message.reply_text("Rewriting…")
        out = await llm_write(prompt)
        await update.effective_message.reply_html(
            f"✍️ <b>REWRITE · {esc(p['name'])}</b>\n\n{esc(out or 'AI offline — set GROQ_API_KEY')}"
        )
        return

    flow = context.user_data.get("add_flow")
    if not flow:
        return
    step = flow.get("step")
    data = flow.setdefault("data", {})
    db, client = deps(context)

    if step == "name":
        data["name"] = text
        flow["step"] = "x"
        await update.effective_message.reply_text("X handle? (@name or URL or - to skip)")
    elif step == "x":
        if text != "-":
            data["x_handle"] = normalize_x(text)
        flow["step"] = "website"
        await update.effective_message.reply_text("Website URL? (or -)")
    elif step == "website":
        if text != "-":
            data["website"] = text
        flow["step"] = "telegram"
        await update.effective_message.reply_text("Telegram link? (or -)")
    elif step == "telegram":
        if text != "-":
            data["telegram"] = normalize_tg(text)
        flow["step"] = "chain"
        await update.effective_message.reply_text("Chain? (solana/base/ethereum/… or -)")
    elif step == "chain":
        if text != "-":
            data["chain"] = text.lower()
        flow["step"] = "contract"
        await update.effective_message.reply_text("Contract address? (or -)")
    elif step == "contract":
        if text != "-":
            data["contract_address"] = text
        pid = await db.add_project(update.effective_user.id, data)
        social = await gather_social(client, data)
        about = (social.get("website") or {}).get("about")
        await db.update_project(
            pid,
            description=about or data.get("description"),
            social_json=json.dumps(social),
            last_social_at=now(),
        )
        context.user_data.pop("add_flow", None)
        await update.effective_message.reply_html(
            f"✅ Saved <b>#{pid} {esc(data['name'])}</b>\n/social {pid} · /ideas {pid}"
        )


async def cmd_projects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message or not update.effective_user:
        return
    db, _ = deps(context)
    rows = await db.list_projects(update.effective_user.id)
    if not rows:
        await update.effective_message.reply_text("No projects yet. /addproject")
        return
    lines = ["📁 <b>YOUR PROJECTS</b>", ""]
    buttons = []
    for p in rows:
        lines.append(
            f"#{p['id']} <b>{esc(p['name'])}</b> · {esc(p.get('chain') or '—')}\n"
            f"𝕏 {esc(p.get('x_handle') or '—')} · 💬 {esc(p.get('telegram') or '—')}"
        )
        buttons.append([InlineKeyboardButton(f"🔍 {p['name'][:28]}", callback_data=f"p:{p['id']}")])
    await update.effective_message.reply_html(
        "\n".join(lines),
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(buttons[:20]),
    )


async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /project <website|@x|tg|CA>")
        return
    p = await resolve_project(update, context, " ".join(context.args))
    if not p:
        await update.effective_message.reply_text("Not found.")
        return
    text = (
        f"📁 <b>#{p['id']} {esc(p['name'])}</b>\n"
        f"Ticker: {esc(p.get('ticker') or '—')} · Chain: {esc(p.get('chain') or '—')}\n"
        f"CA: <code>{esc(p.get('contract_address') or '—')}</code>\n"
        f"🌐 {esc(p.get('website') or '—')}\n"
        f"𝕏 @{esc(p.get('x_handle') or '—')}\n"
        f"💬 {esc(p.get('telegram') or '—')}\n"
        f"Docs: {esc(p.get('docs') or '—')}\n"
        f"About: {esc((p.get('description') or '—')[:400])}\n"
        f"Added: {esc(ago(p.get('date_added')))}"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🐦 Social", callback_data=f"soc:{p['id']}"),
                InlineKeyboardButton("💡 Ideas", callback_data=f"id:{p['id']}"),
            ],
            [
                InlineKeyboardButton("✍️ Write", callback_data=f"wr:{p['id']}"),
                InlineKeyboardButton("⭐ Watch", callback_data=f"wa:{p['id']}"),
            ],
        ]
    )
    await update.effective_message.reply_html(text, disable_web_page_preview=True, reply_markup=kb)


async def cmd_removeproject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message or not update.effective_user:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /removeproject <id>")
        return
    db, _ = deps(context)
    try:
        pid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Numeric id required.")
        return
    await db.remove(update.effective_user.id, pid)
    await update.effective_message.reply_text(f"Removed #{pid}.")


async def cmd_social(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /social <website|@x|t.me/…|CA>")
        return
    p = await resolve_project(update, context, " ".join(context.args))
    if not p:
        await update.effective_message.reply_text("Could not resolve that link/CA.")
        return
    await update.effective_message.reply_text("Collecting public signals…")
    db, client = deps(context)
    social = await gather_social(client, p)
    disc = social.get("discovered_links") or {}
    updates: dict[str, Any] = {"social_json": json.dumps(social), "last_social_at": now()}
    if disc.get("website") and not p.get("website"):
        updates["website"] = disc["website"]
    if disc.get("telegram") and not p.get("telegram"):
        updates["telegram"] = disc["telegram"]
    await db.update_project(p["id"], **updates)
    await db.save_snapshot(p["id"], "social", social)
    p = await db.by_id(p["id"]) or p

    x = social.get("x") or {}
    tg = social.get("telegram") or {}
    web = social.get("website") or {}
    name = p.get("name") or x.get("handle") or "Project"

    # ---- AI layer (runs even if X metrics missing) ----
    ai_ok = False
    ai_raw = ""
    prompt = (
        f"{project_context(p, social)}\n\n"
        "You are building a professional Web3 intelligence brief.\n"
        "Use ONLY the facts above. Never invent follower counts or engagement.\n"
        "Return EXACTLY these section labels on their own lines:\n"
        "WHAT_WE_KNOW\n"
        "AI_ANALYSIS\n"
        "OPEN_QUESTIONS\n"
        "CONTENT_GAPS\n"
        "CONTENT_IDEAS\n"
        "TAKEAWAYS\n"
        "Under CONTENT_GAPS use numbered items with: known / unclear / question / opportunity\n"
        "Under CONTENT_IDEAS use numbered items with: title / format / why\n"
        "Under OPEN_QUESTIONS one question per line.\n"
        "Short bullets. No markdown. No emoji inside body text."
    )
    ai_raw = await llm_write(prompt)
    if ai_raw:
        ai_ok = True
    buckets = parse_labeled_sections(
        ai_raw or "",
        [
            "WHAT_WE_KNOW",
            "AI_ANALYSIS",
            "OPEN_QUESTIONS",
            "CONTENT_GAPS",
            "CONTENT_IDEAS",
            "TAKEAWAYS",
        ],
    )

    # Facts known without AI
    fact_bits = []
    if x.get("bio"):
        fact_bits.append(f"X bio: {x['bio'][:200]}")
    if x.get("handle") or p.get("x_handle"):
        fact_bits.append(f"X handle: @{(x.get('handle') or p.get('x_handle'))}")
    if web.get("title"):
        fact_bits.append(f"Website title: {web['title']}")
    if web.get("about"):
        fact_bits.append(f"Website: {web['about'][:180]}")
    if tg.get("title"):
        fact_bits.append(f"Telegram: {tg['title']}")
    if tg.get("about"):
        fact_bits.append(f"Telegram about: {tg['about'][:160]}")
    if p.get("description"):
        fact_bits.append(f"Stored note: {str(p['description'])[:160]}")
    if disc.get("website"):
        fact_bits.append(f"Link found: {disc['website']}")
    if disc.get("telegram"):
        fact_bits.append(f"Telegram link found: {disc['telegram']}")

    known_ai = bullets_from_block(buckets.get("WHAT_WE_KNOW", ""))
    analysis = bullets_from_block(buckets.get("AI_ANALYSIS", ""))
    questions = bullets_from_block(buckets.get("OPEN_QUESTIONS", ""))
    takeaways = bullets_from_block(buckets.get("TAKEAWAYS", ""))

    blocks: list[str] = [
        dash_header("🐦 SOCIAL INTELLIGENCE", str(name)),
        "",
        "🐦 <b>X</b>",
        f"@{(esc(x.get('handle') or p.get('x_handle') or '—'))}",
        "",
        "📊 <b>ACCOUNT</b>",
        dash_line("Followers", x.get("followers")),
        dash_line("Following", x.get("following")),
        dash_line("Posts", x.get("posts")),
    ]
    if x.get("followers") is None and x.get("following") is None:
        blocks.append("\n⚠️ Live X metrics unavailable")

    blocks += [
        "",
        "🔎 <b>WHAT WE KNOW</b>",
        dash_bullets(fact_bits + known_ai, 10),
        "",
        "🧠 <b>AI ANALYSIS</b>",
    ]
    if ai_ok and analysis:
        blocks.append(dash_bullets(analysis, 8))
    elif ai_ok:
        blocks.append(dash_bullets(["Analysis generated but unstructured — see gaps below."], 3))
    else:
        blocks.append("⚠️ AI analysis unavailable")

    blocks += ["", "❓ <b>OPEN QUESTIONS</b>", dash_questions(questions, 6)]

    # Telegram section
    blocks += [
        "",
        SEP,
        "",
        "💬 <b>TELEGRAM</b>",
        dash_line("Members", tg.get("members")),
        dash_line("Title", tg.get("title")),
        "",
        "🔎 <b>WHAT WE KNOW</b>",
        dash_bullets(
            [x for x in [tg.get("about"), disc.get("telegram"), p.get("telegram")] if x],
            5,
        )
        if (tg.get("about") or disc.get("telegram") or p.get("telegram"))
        else "• —",
    ]

    # Website
    blocks += [
        "",
        SEP,
        "",
        "🌐 <b>WEBSITE</b>",
        dash_line("Title", web.get("title")),
        dash_line("URL", web.get("url") or p.get("website")),
        "",
        "🔎 <b>PROJECT FOCUS</b>",
        dash_bullets([web.get("about")] if web.get("about") else [], 5)
        if web.get("about")
        else "• —",
    ]

    # Content gaps from AI
    blocks += ["", SEP, "", "🧩 <b>CONTENT GAPS</b>", ""]
    gap_body = buckets.get("CONTENT_GAPS", "").strip()
    if gap_body:
        # keep readable, escape
        for ln in gap_body.splitlines()[:40]:
            ln = ln.strip()
            if not ln:
                blocks.append("")
                continue
            low = ln.lower()
            if low.startswith("known") or "what we know" in low or low.startswith("what is known"):
                blocks.append(f"🔎 {esc(ln)}")
            elif "unclear" in low or "missing" in low:
                blocks.append(f"🕳️ {esc(ln)}")
            elif low.startswith("question") or ln.startswith("?"):
                blocks.append(f"❓ {esc(ln.lstrip('?').strip())}")
            elif "opportunity" in low or low.startswith("content"):
                blocks.append(f"💡 {esc(ln)}")
            elif re.match(r"^\d+[.)]", ln):
                blocks.append(f"\n<b>{esc(ln)}</b>")
            else:
                blocks.append(esc(ln))
    else:
        blocks.append("• —" if ai_ok else "⚠️ AI analysis unavailable")

    # Ideas
    blocks += ["", SEP, "", "✍️ <b>CONTENT IDEAS</b>", ""]
    ideas_body = buckets.get("CONTENT_IDEAS", "").strip()
    if ideas_body:
        for ln in ideas_body.splitlines()[:35]:
            ln = ln.strip()
            if not ln:
                continue
            if re.match(r"^\d+[.)]", ln):
                blocks.append(f"\n<b>{esc(ln)}</b>")
            elif low_starts_why(ln):
                blocks.append(f"Why: {esc(re.sub(r'(?i)^why:\\s*', '', ln))}")
            elif low_starts_format(ln):
                blocks.append(f"Format: {esc(re.sub(r'(?i)^format:\\s*', '', ln))}")
            elif low_starts_title(ln):
                blocks.append(f"Title: {esc(re.sub(r'(?i)^title:\\s*', '', ln))}")
            else:
                blocks.append(esc(ln))
    else:
        blocks.append("• —" if ai_ok else "⚠️ AI analysis unavailable")

    blocks += [
        "",
        SEP,
        "",
        "🎯 <b>KEY TAKEAWAYS</b>",
        dash_bullets(takeaways, 5) if takeaways else "• —",
    ]

    lim = user_limitations(social, ai_ok)
    if lim:
        blocks += ["", SEP, "", "⚠️ <b>DATA LIMITATIONS</b>"]
        for L in lim:
            blocks.append(f"• {esc(L)}")

    text = "\n".join(blocks)
    # Telegram hard limit ~4096
    if len(text) > 4000:
        text = text[:3900] + "\n\n…\n<i>Truncated — use /contentgaps or /ideas for detail</i>"

    await update.effective_message.reply_html(
        text,
        disable_web_page_preview=True,
        reply_markup=project_nav_keyboard(int(p["id"])),
    )


def low_starts_why(s: str) -> bool:
    return s.lower().startswith("why")


def low_starts_format(s: str) -> bool:
    return s.lower().startswith("format")


def low_starts_title(s: str) -> bool:
    return s.lower().startswith("title")



async def cmd_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    args = list(context.args or [])
    if not args:
        await update.effective_message.reply_text("Usage: /activity <website|@x|tg|CA> [24h|7d|30d]")
        return
    window = "7d"
    if args[-1] in {"24h", "7d", "30d"}:
        window = args.pop()
    p = await resolve_project(update, context, " ".join(args))
    if not p:
        await update.effective_message.reply_text("Could not resolve.")
        return
    db, client = deps(context)
    social = await gather_social(client, p)
    await db.save_snapshot(p["id"], f"activity_{window}", social)
    x = social.get("x") or {}
    tg = social.get("telegram") or {}
    ai = await llm_write(
        project_context(p, social)
        + f"\n\nPeriod: {window}\nLabel AI_SUMMARY\n3 bullets on activity implications from available facts only. No invented metrics."
    )
    summary = bullets_from_block(parse_labeled_sections(ai or "", ["AI_SUMMARY"]).get("AI_SUMMARY", "") or (ai or ""))
    blocks = [
        dash_header("📊 SOCIAL ACTIVITY", str(p.get("name") or "")),
        f"Period: {esc(window)}",
        "",
        "🐦 <b>X</b>",
        dash_line("Followers", x.get("followers")),
        dash_line("Following", x.get("following")),
        dash_line("Posts", x.get("posts")),
        "Posting: —",
        "Engagement: —",
        "Follower change: —",
        "",
        "💬 <b>TELEGRAM</b>",
        dash_line("Members", tg.get("members")),
        "Activity: —",
        "",
        "🧠 <b>AI SUMMARY</b>",
        dash_bullets(summary, 5) if summary else "⚠️ AI analysis unavailable",
        "",
        "⚠️ <b>DATA LIMITATIONS</b>",
        "• Historical engagement requires repeated snapshots over time.",
        "• Live X metrics unavailable." if x.get("followers") is None else "• Snapshot stored for future comparison.",
    ]
    await update.effective_message.reply_html(
        "\n".join(blocks), disable_web_page_preview=True, reply_markup=project_nav_keyboard(int(p["id"]))
    )



async def cmd_ideas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    args = list(context.args or [])
    if not args:
        await update.effective_message.reply_text(
            "Usage: /ideas <website|@x|CA> [persona]\nPersonas: founder community educator analyst degen minimal"
        )
        return
    persona = "community"
    if args[-1].lower() in PERSONAS:
        persona = args.pop().lower()
    p = await resolve_project(update, context, " ".join(args))
    if not p:
        await update.effective_message.reply_text("Could not resolve.")
        return
    _, client = deps(context)
    social = await gather_social(client, p)
    await update.effective_message.reply_text(f"Ideas · {persona}…")
    voice = PERSONAS.get(persona, PERSONAS["community"])
    prompt = (
        f"{project_context(p, social)}\n\nPersona: {persona}\n{voice}\n"
        f"Seed {random.randint(1,9999)}\n\n"
        "Give exactly 6 content ideas. For each use this plain format:\n"
        "1. TITLE\nFormat: thread|short post|TG|video\nAngle: one line why it fits THIS project\nDraft hook: one ready sentence\n\n"
        "No markdown headers. No generic crypto filler."
    )
    out = await llm_write(prompt)
    body = format_ideas_clean(out) if out else esc(_ai_fail())
    await update.effective_message.reply_html(
        f"💡 <b>CONTENT IDEAS</b>\n\nProject: <b>{esc(p.get('name') or '')}</b>\nPersona: {esc(persona)}\n\n{SEP}\n\n{body}",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔀 Shuffle ideas", callback_data=f"shi:{p['id']}:{persona}"
                    ),
                    InlineKeyboardButton("👤 Personas", callback_data=f"per:{p['id']}"),
                ],
                [
                    InlineKeyboardButton("✍️ Write", callback_data=f"wr:{p['id']}"),
                    InlineKeyboardButton("« Social", callback_data=f"soc:{p['id']}"),
                ],
            ]
        ),
    )



async def cmd_write(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    args = list(context.args or [])
    if not args:
        await update.effective_message.reply_text(
            "Usage:\n/write <website|@x|CA> [x|thread|tg|edu|community|announce] [persona]\n"
            "Personas: founder community educator analyst degen minimal\n"
            "Or open Personas from the project menu."
        )
        return
    kind = "x"
    persona = "community"
    # trailing tokens
    known_kinds = {"x", "thread", "tg", "edu", "community", "announce", "reply"}
    while args and args[-1].lower() in PERSONAS:
        persona = args.pop().lower()
    if args and args[-1].lower() in known_kinds:
        kind = args.pop().lower()
    q = " ".join(args)
    p = await resolve_project(update, context, q)
    if not p:
        await update.effective_message.reply_text("Could not resolve.")
        return
    await update.effective_message.reply_text(f"Writing · {kind} · {persona}…")
    text = await generate_write(p, kind, persona, context)
    await update.effective_message.reply_html(
        text,
        disable_web_page_preview=True,
        reply_markup=write_nav_keyboard(int(p["id"]), kind, persona),
    )


async def generate_write(p: dict[str, Any], kind: str, persona: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    client = context.application.bot_data["http"]
    social = await gather_social(client, p)
    voice = PERSONAS.get(persona) or PERSONAS["community"]
    seed = random.randint(1000, 9999)
    prompt = (
        f"{project_context(p, social)}\n\n"
        f"Persona: {persona}\n{voice}\n"
        f"Format: {kind}\nVariation seed: {seed}\n\n"
        "Return EXACTLY:\n"
        "POST\n"
        "(the full copy-ready post or thread only — no emoji section markers)\n"
        "ALTERNATE\n"
        "(second full version)\n"
        "WHY\n"
        "(3 short bullets on strategy)\n"
        "No hashtag spam. Do not invent product claims."
    )
    out = await llm_write(prompt)
    if not out:
        return (
            f"✍️ <b>WRITE</b>\n\nProject: <b>{esc(p.get('name') or '')}</b>\n\n"
            f"⚠️ AI analysis unavailable"
        )
    buckets = parse_labeled_sections(out, ["POST", "ALTERNATE", "WHY"])
    post = buckets.get("POST", "").strip() or strip_md(out).split("ALTERNATE")[0].strip()
    alt = buckets.get("ALTERNATE", "").strip()
    why = bullets_from_block(buckets.get("WHY", ""))
    parts = [
        f"✍️ <b>{esc(kind.upper())}</b>",
        f"Project: <b>{esc(p.get('name') or '')}</b>",
        f"Persona: {esc(persona)}",
        "",
        SEP,
        "",
        "✍️ <b>COPY</b>",
        "",
        copyable(post[:3500]),
    ]
    if alt:
        parts += ["", "✨ <b>ALTERNATE</b>", "", copyable(alt[:2500])]
    if why:
        parts += ["", "🔎 <b>WHY THIS WORKS</b>", dash_bullets(why, 5)]
    parts += ["", "Tap Shuffle for another take · Personas to change voice"]
    return "\n".join(parts)



def write_nav_keyboard(pid: int, kind: str, persona: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔀 Shuffle", callback_data=f"shw:{pid}:{kind}:{persona}"
                ),
                InlineKeyboardButton("👤 Personas", callback_data=f"per:{pid}"),
            ],
            [
                InlineKeyboardButton("« Social", callback_data=f"soc:{pid}"),
                InlineKeyboardButton("💡 Ideas", callback_data=f"id:{pid}"),
            ],
        ]
    )



async def cmd_thread(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    args = list(context.args or [])
    if len(args) < 2:
        await update.effective_message.reply_text(
            "Usage: /thread <website|@x|CA> <topic> [persona]"
        )
        return
    persona = "community"
    if args[-1].lower() in PERSONAS:
        persona = args.pop().lower()
    p = await resolve_project(update, context, args[0])
    topic = " ".join(args[1:])
    if not p:
        p = await resolve_project(update, context, " ".join(args[:2]))
        topic = " ".join(args[2:])
    if not p or not topic:
        await update.effective_message.reply_text("Need project + topic.")
        return
    _, client = deps(context)
    social = await gather_social(client, p)
    await update.effective_message.reply_text(f"Thread · {persona}…")
    voice = PERSONAS.get(persona, PERSONAS["community"])
    prompt = (
        f"{project_context(p, social)}\n\nTopic: {topic}\nPersona: {persona}\n{voice}\n"
        f"Seed {random.randint(1,9999)}\n\n"
        "Write a 7-tweet thread. Label each line as 1/7 through 7/7. "
        "Copy-ready. No hashtag spam. Factual to project context only."
    )
    out = await llm_write(prompt)
    body = copy_block(out) if out else esc(_ai_fail())
    await update.effective_message.reply_html(
        f"🧵 <b>THREAD · {esc(p.get('name') or '')}</b> · <i>{esc(persona)}</i>\n"
        f"Topic: {esc(topic)}\n\n{body}",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔀 Shuffle",
                        callback_data=f"sht:{p['id']}:{persona}:{topic[:40]}",
                    ),
                    InlineKeyboardButton("👤 Personas", callback_data=f"per:{p['id']}"),
                ],
                [InlineKeyboardButton("« Back", callback_data=f"p:{p['id']}")],
            ]
        ),
    )



async def cmd_rewrite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /rewrite <website|@x|CA>  then paste the text")
        return
    p = await resolve_project(update, context, " ".join(context.args))
    if not p:
        await update.effective_message.reply_text("Not found.")
        return
    context.user_data["rewrite_pid"] = p["id"]
    await update.effective_message.reply_text(
        f"Paste the content to rewrite for {p['name']}.\n(/cancel to abort)"
    )


async def cmd_contentgaps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    args = list(context.args or [])
    if not args:
        await update.effective_message.reply_text("Usage: /contentgaps <website|@x|tg|CA>")
        return
    p = await resolve_project(update, context, " ".join(args))
    if not p:
        await update.effective_message.reply_text("Could not resolve.")
        return
    _, client = deps(context)
    social = await gather_social(client, p)
    await update.effective_message.reply_text("Analyzing gaps…")
    prompt = (
        f"{project_context(p, social)}\n\n"
        "Evidence-based content gaps only. Labels:\n"
        "KNOWN\nUNCLEAR\nQUESTIONS\nOPPORTUNITIES\n"
        "For OPPORTUNITIES each item: title / why (tied to a gap). No generic advice. No emoji in body."
    )
    out = await llm_write(prompt)
    buckets = parse_labeled_sections(out or "", ["KNOWN", "UNCLEAR", "QUESTIONS", "OPPORTUNITIES"])
    blocks = [
        dash_header("🧩 CONTENT GAPS", str(p.get("name") or "")),
        "",
        "🔎 <b>WHAT WE KNOW</b>",
        dash_bullets(bullets_from_block(buckets.get("KNOWN", "")), 8),
        "",
        "🕳️ <b>WHAT IS UNCLEAR</b>",
        dash_bullets(bullets_from_block(buckets.get("UNCLEAR", "")), 8),
        "",
        "❓ <b>QUESTIONS</b>",
        dash_questions(bullets_from_block(buckets.get("QUESTIONS", "")), 8),
        "",
        "💡 <b>CONTENT OPPORTUNITIES</b>",
        dash_bullets(bullets_from_block(buckets.get("OPPORTUNITIES", "")), 8),
    ]
    if not out:
        blocks = [
            dash_header("🧩 CONTENT GAPS", str(p.get("name") or "")),
            "",
            "⚠️ AI analysis unavailable",
        ]
    await update.effective_message.reply_html(
        "\n".join(blocks),
        disable_web_page_preview=True,
        reply_markup=project_nav_keyboard(int(p["id"])),
    )



async def cmd_announce(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    args = context.args or []
    if len(args) < 2:
        await update.effective_message.reply_text("Usage: /announce <website|@x|CA> <what happened>")
        return
    p = await resolve_project(update, context, args[0])
    what = " ".join(args[1:])
    if not p:
        p = await resolve_project(update, context, " ".join(args[:2]))
        what = " ".join(args[2:])
    if not p or not what:
        await update.effective_message.reply_text("Need project + announcement subject.")
        return
    _, client = deps(context)
    social = await gather_social(client, p)
    prompt = (
        f"{project_context(p, social)}\n\nAnnouncement subject: {what}\n\n"
        "Write: 1) X post 2) Telegram announcement 3) short Discord-style note. "
        "Professional, no hype spam."
    )
    out = await llm_write(prompt)
    await update.effective_message.reply_html(
        f"📢 <b>ANNOUNCE · {esc(p['name'])}</b>\n\n<code>{esc(out or 'AI offline')}</code>"
    )


async def cmd_communitycontent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /communitycontent <website|@x|tg|CA>")
        return
    p = await resolve_project(update, context, " ".join(context.args))
    if not p:
        await update.effective_message.reply_text("Not found.")
        return
    _, client = deps(context)
    social = await gather_social(client, p)
    prompt = (
        f"{project_context(p, social)}\n\n"
        "Generate community engagement content: 3 discussion questions, 2 poll ideas, "
        "2 feedback requests, 1 challenge. Specific to this product."
    )
    out = await llm_write(prompt)
    await update.effective_message.reply_html(
        f"👥 <b>COMMUNITY CONTENT · {esc(p['name'])}</b>\n\n{esc(out or 'AI offline')}"
    )


async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /voice <id|name>\nor /voice <id> tone=concise technical=medium humor=low"
        )
        return
    db, client = deps(context)
    # parse trailing key=value
    tokens = context.args
    kv = {}
    name_parts = []
    for t in tokens:
        if "=" in t:
            k, v = t.split("=", 1)
            kv[k.strip()] = v.strip()
        else:
            name_parts.append(t)
    p = await resolve_project(update, context, " ".join(name_parts) if name_parts else tokens[0])
    if not p:
        await update.effective_message.reply_text("Not found.")
        return
    voice = {}
    try:
        voice = json.loads(p.get("voice_json") or "{}")
    except Exception:
        pass
    if kv:
        voice.update(kv)
        await db.update_project(p["id"], voice_json=json.dumps(voice))
        await update.effective_message.reply_html(
            f"🎙 Voice updated for <b>{esc(p['name'])}</b>\n<code>{esc(json.dumps(voice))}</code>"
        )
        return
    if not voice:
        social = await gather_social(client, p)
        prompt = (
            f"{project_context(p, social)}\n\n"
            "Infer a voice profile (tone, formality, technical_level, humor, emoji_usage, CTA_style). "
            "JSON only."
        )
        out = await llm_write(prompt)
        await update.effective_message.reply_html(
            f"🎙 <b>VOICE · {esc(p['name'])}</b>\n\n{esc(out or 'Set with /voice id tone=…')}"
        )
    else:
        await update.effective_message.reply_html(
            f"🎙 <b>VOICE · {esc(p['name'])}</b>\n<code>{esc(json.dumps(voice, indent=2))}</code>"
        )


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /daily <website|@x|tg|CA>")
        return
    p = await resolve_project(update, context, " ".join(context.args))
    if not p:
        await update.effective_message.reply_text("Not found.")
        return
    _, client = deps(context)
    social = await gather_social(client, p)
    prompt = (
        f"{project_context(p, social)}\n\n"
        "Write a DAILY INTELLIGENCE brief with sections:\n"
        "New public signals · Community · Content opportunity · Unanswered question risk\n"
        "Separate FACT vs ANALYSIS."
    )
    out = await llm_write(prompt)
    await update.effective_message.reply_html(
        f"📅 <b>DAILY · {esc(p['name'])}</b>\n\n{esc(out or 'AI offline')}"
    )


async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /audit <website|@x|tg|CA>")
        return
    p = await resolve_project(update, context, " ".join(context.args))
    if not p:
        await update.effective_message.reply_text("Not found.")
        return
    _, client = deps(context)
    social = await gather_social(client, p)
    prompt = (
        f"{project_context(p, social)}\n\n"
        "Produce a SOCIAL & CONTENT AUDIT: consistency, content mix, messaging clarity, "
        "audience response risks, recommendations. Cite only given facts; mark speculation."
    )
    out = await llm_write(prompt)
    await update.effective_message.reply_html(
        f"📊 <b>AUDIT · {esc(p['name'])}</b>\n\n{esc(out or 'AI offline')}"
    )


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message or not update.effective_user:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /watch <website|@x|tg|CA>")
        return
    p = await resolve_project(update, context, " ".join(context.args))
    if not p:
        await update.effective_message.reply_text("Not found.")
        return
    db, _ = deps(context)
    await db.watch(update.effective_user.id, p["id"])
    await update.effective_message.reply_html(f"⭐ Watching <b>#{p['id']} {esc(p['name'])}</b>")


async def cmd_unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message or not update.effective_user:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /unwatch <id>")
        return
    db, _ = deps(context)
    p = await resolve_project(update, context, context.args[0])
    if not p:
        await update.effective_message.reply_text("Not found.")
        return
    await db.unwatch(update.effective_user.id, p["id"])
    await update.effective_message.reply_text(f"Removed #{p['id']} from watchlist.")


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message or not update.effective_user:
        return
    db, _ = deps(context)
    rows = await db.watchlist(update.effective_user.id)
    if not rows:
        await update.effective_message.reply_text("Watchlist empty.")
        return
    lines = ["⭐ <b>WATCHLIST</b>"] + [f"#{p['id']} {esc(p['name'])}" for p in rows]
    await update.effective_message.reply_html("\n".join(lines))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data or not update.effective_user:
        return
    if not await gate(update, context):
        return
    try:
        await q.answer()
    except Exception:
        pass
    data = q.data
    parts = data.split(":")
    kind = parts[0]
    if len(parts) < 2 or not parts[1].isdigit():
        return
    pid = int(parts[1])
    db, client = deps(context)
    p = await db.by_id(pid)
    if not p:
        await q.edit_message_text("Project not found. Run the command again with the link.")
        return

    if kind == "p":
        text = (
            f"📁 <b>#{p['id']} {esc(p['name'])}</b>\n"
            f"🌐 {esc(p.get('website') or '—')}\n"
            f"𝕏 @{esc(p.get('x_handle') or '—')}\n"
            f"💬 {esc(p.get('telegram') or '—')}"
        )
        await q.edit_message_text(
            text, parse_mode="HTML", reply_markup=project_nav_keyboard(pid), disable_web_page_preview=True
        )
    elif kind == "soc":
        context.args = [str(pid)]
        # send as new message for long content
        fake = update
        await cmd_social(update, context)
    elif kind == "id":
        context.args = [str(pid)]
        await cmd_ideas(update, context)
    elif kind == "wr":
        context.args = [str(pid), "x", "community"]
        await cmd_write(update, context)
    elif kind == "shw":
        knd = parts[2] if len(parts) > 2 else "x"
        persona = parts[3] if len(parts) > 3 else "community"
        await q.message.reply_text(f"Shuffling · {knd} · {persona}…")
        text = await generate_write(p, knd, persona, context)
        await q.message.reply_html(
            text, disable_web_page_preview=True, reply_markup=write_nav_keyboard(pid, knd, persona)
        )
    elif kind == "per":
        await q.edit_message_text(
            f"👤 <b>Pick a writing persona</b>\n{esc(p.get('name') or '')}",
            parse_mode="HTML",
            reply_markup=persona_keyboard(pid),
        )
    elif kind == "pw":
        persona = parts[2] if len(parts) > 2 else "community"
        await q.message.reply_text(f"Writing as {persona}…")
        text = await generate_write(p, "x", persona, context)
        await q.message.reply_html(
            text,
            disable_web_page_preview=True,
            reply_markup=write_nav_keyboard(pid, "x", persona),
        )
    elif kind == "gap":
        context.args = [str(pid)]
        await cmd_contentgaps(update, context)
    elif kind == "cal":
        context.args = [str(pid)]
        await cmd_calendar(update, context)
    elif kind == "shi":
        persona = parts[2] if len(parts) > 2 else "community"
        context.args = [str(pid), persona]
        await cmd_ideas(update, context)
    elif kind == "sht":
        persona = parts[2] if len(parts) > 2 else "community"
        topic = ":".join(parts[3:]) if len(parts) > 3 else "product overview"
        context.args = [str(pid), topic, persona]
        await cmd_thread(update, context)
    elif kind == "wa":
        await db.watch(update.effective_user.id, pid)
        await q.answer("Watching", show_alert=True)



async def on_start_app(app: Application) -> None:
    db = DB(os.getenv("DATABASE_PATH", "./social.db"))
    await db.connect()
    app.bot_data["db"] = db
    app.bot_data["http"] = httpx.AsyncClient(timeout=30, follow_redirects=True)
    raw = os.getenv("ALLOWED_USER_IDS", "").strip()
    if raw:
        app.bot_data["allowed"] = [int(x) for x in raw.split(",") if x.strip().isdigit()]
    else:
        existing = await db.get_meta("owner_id")
        app.bot_data["allowed"] = [int(existing)] if existing else []
    log.info("Social+Content bot ready")


async def on_stop_app(app: Application) -> None:
    db: DB = app.bot_data.get("db")
    http: httpx.AsyncClient = app.bot_data.get("http")
    if http:
        await http.aclose()
    if db:
        await db.close()



async def cmd_narratives(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /narratives <website|@x|tg|CA>")
        return
    p = await resolve_project(update, context, " ".join(context.args))
    if not p:
        await update.effective_message.reply_text("Could not resolve.")
        return
    _, client = deps(context)
    social = await gather_social(client, p)
    prompt = (
        f"{project_context(p, social)}\n\n"
        "NARRATIVE TRACKER. Sections:\n"
        "PROJECT NARRATIVES (what they seem to push)\n"
        "COMMUNITY NARRATIVES (likely questions/themes from public about text)\n"
        "EXTERNAL / MARKET NARRATIVES (relevant crypto themes)\n"
        "CONTENT INTERSECTION (1-2 angles)\n"
        "Mark analysis vs fact. No price talk."
    )
    out = await llm_write(prompt)
    await update.effective_message.reply_html(
        f"🔥 <b>NARRATIVES · {esc(p['name'])}</b>\n\n{esc(out or _ai_fail())}"
    )


async def cmd_audience(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /audience <website|@x|tg|CA>")
        return
    p = await resolve_project(update, context, " ".join(context.args))
    if not p:
        await update.effective_message.reply_text("Could not resolve.")
        return
    _, client = deps(context)
    social = await gather_social(client, p)
    prompt = (
        f"{project_context(p, social)}\n\n"
        "AUDIENCE INTELLIGENCE from public signals only:\n"
        "Themes · Questions · Concerns · Interests · Sophistication (guess labeled as analysis)\n"
        "Do not invent demographics."
    )
    out = await llm_write(prompt)
    await update.effective_message.reply_html(
        f"👥 <b>AUDIENCE · {esc(p['name'])}</b>\n\n{esc(out or _ai_fail())}"
    )


async def cmd_topcontent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /topcontent <website|@x|tg|CA>")
        return
    p = await resolve_project(update, context, " ".join(context.args))
    if not p:
        await update.effective_message.reply_text("Could not resolve.")
        return
    _, client = deps(context)
    social = await gather_social(client, p)
    prompt = (
        f"{project_context(p, social)}\n\n"
        "Without live engagement metrics, infer likely TOP CONTENT angles for this project "
        "and what usually works for similar products. Label clearly as strategy inference, not measured stats.\n"
        "3 formats with: topic, format, why it may work, suggested hook."
    )
    out = await llm_write(prompt)
    await update.effective_message.reply_html(
        f"🔥 <b>TOP CONTENT ANGLES · {esc(p['name'])}</b>\n\n{esc(out or _ai_fail())}"
    )


async def cmd_contentpatterns(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /contentpatterns <website|@x|tg|CA>")
        return
    p = await resolve_project(update, context, " ".join(context.args))
    if not p:
        await update.effective_message.reply_text("Could not resolve.")
        return
    _, client = deps(context)
    social = await gather_social(client, p)
    prompt = (
        f"{project_context(p, social)}\n\n"
        "CONTENT PATTERNS report:\nStrong topics · Strong formats · Likely audience responses · "
        "Low-engagement patterns to avoid · Posting cadence suggestion\n"
        "Base on project type + public copy; say when data is thin."
    )
    out = await llm_write(prompt)
    await update.effective_message.reply_html(
        f"🧠 <b>CONTENT PATTERNS · {esc(p['name'])}</b>\n\n{esc(out or _ai_fail())}"
    )


async def cmd_mentions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /mentions <website|@x|tg|CA>")
        return
    p = await resolve_project(update, context, " ".join(context.args))
    if not p:
        await update.effective_message.reply_text("Could not resolve.")
        return
    _, client = deps(context)
    social = await gather_social(client, p)
    prompt = (
        f"{project_context(p, social)}\n\n"
        "MENTION / CONVERSATION intelligence (inferred from public positioning, not a live mention crawl):\n"
        "Likely positive themes · Neutral · Concerns · Questions\n"
        "State clearly this is inference until X search is configured."
    )
    out = await llm_write(prompt)
    await update.effective_message.reply_html(
        f"🗣 <b>MENTIONS (inferred) · {esc(p['name'])}</b>\n\n{esc(out or _ai_fail())}"
    )


async def cmd_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /weekly <website|@x|tg|CA>")
        return
    p = await resolve_project(update, context, " ".join(context.args))
    if not p:
        await update.effective_message.reply_text("Could not resolve.")
        return
    _, client = deps(context)
    social = await gather_social(client, p)
    prompt = (
        f"{project_context(p, social)}\n\n"
        "WEEKLY REPORT outline for a community operator:\n"
        "Social snapshot · Activity · Top content ideas · Weak spots · "
        "Community questions · Narratives · Content gaps · Next week plan (5 bullets)"
    )
    out = await llm_write(prompt)
    await update.effective_message.reply_html(
        f"🗓 <b>WEEKLY · {esc(p['name'])}</b>\n\n{esc(out or _ai_fail())}"
    )


async def cmd_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /calendar <website|@x|tg|CA>")
        return
    p = await resolve_project(update, context, " ".join(context.args))
    if not p:
        await update.effective_message.reply_text("Could not resolve.")
        return
    _, client = deps(context)
    social = await gather_social(client, p)
    prompt = (
        f"{project_context(p, social)}\n\n"
        "7-day CONTENT CALENDAR (Mon-Sun). Each day: theme + 1 specific post idea for THIS project. "
        "Not generic crypto filler."
    )
    out = await llm_write(prompt)
    await update.effective_message.reply_html(
        f"📅 <b>CALENDAR · {esc(p['name'])}</b>\n\n{esc(out or _ai_fail())}"
    )


async def cmd_repurpose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /repurpose <website|@x|CA>\nThen paste the blog/announcement/thread text."
        )
        return
    p = await resolve_project(update, context, " ".join(context.args))
    if not p:
        await update.effective_message.reply_text("Could not resolve.")
        return
    context.user_data["repurpose_pid"] = p["id"]
    await update.effective_message.reply_text(
        f"Paste the source content to repurpose for {p['name']}.\n(/cancel to abort)"
    )


def _ai_fail() -> str:
    bits = [f"{k}: {v}" for k, v in KEY_STATUS.items()] or ["no attempt recorded"]
    return (
        "AI offline.\n"
        + "\n".join(bits)
        + "\n\nRun /testai — keys must be on THIS Railway service, then Redeploy."
    )



def main() -> None:
    token = env("TELEGRAM_BOT_TOKEN", "SOCIAL_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN")
    app = (
        Application.builder()
        .token(token)
        .post_init(on_start_app)
        .post_shutdown(on_stop_app)
        .build()
    )
    cmds = [
        ("start", cmd_start),
        ("help", cmd_help),
        ("status", cmd_status),
        ("addproject", cmd_addproject),
        ("cancel", cmd_cancel),
        ("projects", cmd_projects),
        ("project", cmd_project),
        ("removeproject", cmd_removeproject),
        ("social", cmd_social),
        ("activity", cmd_activity),
        ("ideas", cmd_ideas),
        ("write", cmd_write),
        ("thread", cmd_thread),
        ("rewrite", cmd_rewrite),
        ("contentgaps", cmd_contentgaps),
        ("announce", cmd_announce),
        ("communitycontent", cmd_communitycontent),
        ("voice", cmd_voice),
        ("daily", cmd_daily),
        ("audit", cmd_audit),
        ("watch", cmd_watch),
        ("unwatch", cmd_unwatch),
        ("watchlist", cmd_watchlist),
    ]
    v2 = [
        ("narratives", cmd_narratives),
        ("audience", cmd_audience),
        ("topcontent", cmd_topcontent),
        ("contentpatterns", cmd_contentpatterns),
        ("mentions", cmd_mentions),
        ("weekly", cmd_weekly),
        ("calendar", cmd_calendar),
        ("repurpose", cmd_repurpose),
        ("testai", cmd_testai),
        ("debug", cmd_debug),
        ("settings", cmd_status),
    ]
    cmds.extend(v2)
    for name, fn in cmds:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
