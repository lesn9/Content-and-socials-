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
        v = (os.getenv(n) or "").strip()
        if v:
            return v
    return default


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

<b>System</b>
/status · /help

Works with whatever is available (X only, TG only, site only, or CA only).
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


async def llm_write(prompt: str, system: str | None = None) -> str:
    """Free-first: Groq → OpenRouter → Gemini → xAI."""
    sys_msg = system or (
        "You are a Web3 social & content strategist. Be specific to the project. "
        "Never invent product facts not given in the prompt. No investment advice. "
        "Plain text, scannable bullets when useful."
    )
    providers = []
    groq = env("GROQ_API_KEY")
    if groq:
        providers.append(
            ("groq", groq, "https://api.groq.com/openai/v1/chat/completions", "llama-3.3-70b-versatile")
        )
    ork = env("OPENROUTER_API_KEY")
    if ork:
        providers.append(
            (
                "openrouter",
                ork,
                "https://openrouter.ai/api/v1/chat/completions",
                "meta-llama/llama-3.3-70b-instruct:free",
            )
        )
    gem = env("GEMINI_API_KEY", "GOOGLE_API_KEY")
    xai = env("XAI_API_KEY")
    oai = env("OPENAI_API_KEY")

    async with httpx.AsyncClient(timeout=60) as client:
        for name, key, url, model in providers:
            try:
                r = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": sys_msg},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.7,
                    },
                )
                if r.status_code >= 400:
                    KEY_STATUS[name] = f"http {r.status_code}"
                    continue
                data = r.json()
                text = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
                if text:
                    KEY_STATUS[name] = f"ok:{model}"
                    return text
            except Exception as exc:
                KEY_STATUS[name] = str(exc)[:80]
                log.warning("%s failed: %s", name, exc)

        if gem:
            try:
                model = "gemini-2.0-flash"
                r = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    params={"key": gem},
                    json={"contents": [{"parts": [{"text": sys_msg + "\n\n" + prompt}]}]},
                )
                if r.status_code < 400:
                    data = r.json()
                    parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
                    text = "\n".join(p.get("text") or "" for p in parts).strip()
                    if text:
                        KEY_STATUS["gemini"] = f"ok:{model}"
                        return text
                else:
                    KEY_STATUS["gemini"] = f"http {r.status_code}"
            except Exception as exc:
                KEY_STATUS["gemini"] = str(exc)[:80]

        if xai or oai:
            key = xai or oai
            base = "https://api.x.ai/v1/chat/completions" if xai else "https://api.openai.com/v1/chat/completions"
            model = "grok-2-latest" if xai else "gpt-4o-mini"
            try:
                r = await client.post(
                    base,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": sys_msg},
                            {"role": "user", "content": prompt},
                        ],
                    },
                )
                if r.status_code < 400:
                    data = r.json()
                    text = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
                    if text:
                        KEY_STATUS["xai" if xai else "openai"] = f"ok:{model}"
                        return text
                KEY_STATUS["xai" if xai else "openai"] = f"http {r.status_code}"
            except Exception as exc:
                KEY_STATUS["xai" if xai else "openai"] = str(exc)[:80]

    return ""


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
    """Best-effort: official API if bearer present, else note unavailable."""
    out: dict[str, Any] = {
        "handle": handle,
        "followers": None,
        "following": None,
        "posts": None,
        "bio": None,
        "ok": False,
        "note": None,
    }
    h = normalize_x(handle)
    if not h:
        return out
    out["handle"] = h
    bearer = env("X_BEARER_TOKEN")
    if not bearer:
        out["note"] = "X API not configured — set X_BEARER_TOKEN for live metrics"
        return out
    try:
        r = await client.get(
            f"https://api.x.com/2/users/by/username/{h}",
            params={"user.fields": "public_metrics,description"},
            headers={"Authorization": f"Bearer {bearer}"},
            timeout=20,
        )
        if r.status_code >= 400:
            out["note"] = f"X API http {r.status_code}"
            return out
        data = (r.json().get("data") or {})
        metrics = data.get("public_metrics") or {}
        out["followers"] = metrics.get("followers_count")
        out["following"] = metrics.get("following_count")
        out["posts"] = metrics.get("tweet_count")
        out["bio"] = data.get("description")
        out["ok"] = True
    except Exception as e:
        out["note"] = str(e)[:100]
    return out


async def gather_social(client: httpx.AsyncClient, p: dict[str, Any]) -> dict[str, Any]:
    website = await fetch_website_brief(client, p.get("website"))
    tg = await fetch_tg_brief(client, normalize_tg(p.get("telegram")))
    x = await fetch_x_brief(client, p.get("x_handle") or p.get("twitter"))
    return {
        "website": website,
        "telegram": tg,
        "x": x,
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
        await update.effective_message.reply_text("Usage: /social <website|@x|t.me/…|CA|chain:CA>")
        return
    p = await resolve_project(update, context, " ".join(context.args))
    if not p:
        await update.effective_message.reply_text("Could not resolve that link/CA.")
        return
    await update.effective_message.reply_text("Gathering public social signals…")
    db, client = deps(context)
    social = await gather_social(client, p)
    await db.update_project(p["id"], social_json=json.dumps(social), last_social_at=now())
    await db.save_snapshot(p["id"], "social", social)

    x = social.get("x") or {}
    tg = social.get("telegram") or {}
    web = social.get("website") or {}

    lines = [
        "🐦 <b>SOCIAL INTELLIGENCE</b>",
        f"Project: <b>{esc(p['name'])}</b> (#{p['id']})",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "<b>X</b>",
        f"Handle: @{esc(x.get('handle') or p.get('x_handle') or '—')}",
        f"Followers: {esc(x.get('followers') if x.get('followers') is not None else '—')}",
        f"Following: {esc(x.get('following') if x.get('following') is not None else '—')}",
        f"Posts: {esc(x.get('posts') if x.get('posts') is not None else '—')}",
        f"Bio: {esc((x.get('bio') or x.get('note') or '—')[:200])}",
        "",
        "<b>Telegram</b>",
        f"Title: {esc(tg.get('title') or '—')}",
        f"Members: {esc(tg.get('members') if tg.get('members') is not None else '—')}",
        f"About: {esc((tg.get('about') or '—')[:200])}",
        "",
        "<b>Website</b>",
        f"Title: {esc(web.get('title') or '—')}",
        f"About: {esc((web.get('about') or '—')[:220])}",
        f"Last check: {esc(ago(social.get('gathered_at')))}",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    # AI layer on top of facts
    prompt = (
        f"{project_context(p, social)}\n\n"
        "Based ONLY on the facts above, write short sections:\n"
        "TREND (1 line)\nMAIN TOPICS (up to 3 bullets)\nAUDIENCE REACTION (1-2 lines)\n"
        "ATTENTION (gaps/unanswered risks)\nCONTENT OPPORTUNITY (1 concrete idea)\n"
        "Label interpretation as analysis, not fact."
    )
    analysis = await llm_write(prompt)
    if analysis:
        lines.append(esc(analysis))
    else:
        lines.append("⚠️ AI offline — raw public data only. Set GROQ_API_KEY.")
    await update.effective_message.reply_html("\n".join(lines), disable_web_page_preview=True)


async def cmd_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    args = context.args or []
    if not args:
        await update.effective_message.reply_text("Usage: /activity <website|@x|tg|CA> [24h|7d|30d]")
        return
    window = "7d"
    if args[-1] in {"24h", "7d", "30d"}:
        window = args[-1]
        q = " ".join(args[:-1])
    else:
        q = " ".join(args)
    p = await resolve_project(update, context, q)
    if not p:
        await update.effective_message.reply_text("Not found.")
        return
    db, client = deps(context)
    social = await gather_social(client, p)
    await db.save_snapshot(p["id"], f"activity_{window}", social)
    text = (
        f"📊 <b>ACTIVITY · {esc(window)}</b>\n"
        f"{esc(p['name'])}\n\n"
        f"V1 stores snapshots each time you run this.\n"
        f"X followers now: {esc((social.get('x') or {}).get('followers') or '—')}\n"
        f"TG members now: {esc((social.get('telegram') or {}).get('members') or '—')}\n"
        f"Site ok: {esc((social.get('website') or {}).get('ok'))}\n\n"
        "Historical comparison improves as you re-run /activity over days (V2 charts)."
    )
    await update.effective_message.reply_html(text)


async def cmd_ideas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /ideas <website|@x|tg|CA>")
        return
    p = await resolve_project(update, context, " ".join(context.args))
    if not p:
        await update.effective_message.reply_text("Not found.")
        return
    _, client = deps(context)
    social = await gather_social(client, p)
    await update.effective_message.reply_text("Generating ideas…")
    prompt = (
        f"{project_context(p, social)}\n\n"
        "Generate 6 project-specific content opportunities. "
        "For each: title, format (thread/short post/TG/video idea), why it fits THIS project. "
        "No generic crypto filler. Seed "
        f"{random.randint(1,9999)}."
    )
    out = await llm_write(prompt)
    await update.effective_message.reply_html(
        f"💡 <b>CONTENT OPPORTUNITIES · {esc(p['name'])}</b>\n\n{esc(out or 'AI offline')}",
        disable_web_page_preview=True,
    )


async def cmd_write(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    args = context.args or []
    if not args:
        await update.effective_message.reply_text(
            "Usage: /write <website|@x|tg|CA> [x|thread|tg|edu|community|announce]"
        )
        return
    kind = "x"
    if args[-1].lower() in {"x", "thread", "tg", "edu", "community", "announce", "reply"}:
        kind = args[-1].lower()
        q = " ".join(args[:-1])
    else:
        q = " ".join(args)
    p = await resolve_project(update, context, q)
    if not p:
        await update.effective_message.reply_text("Not found.")
        return
    _, client = deps(context)
    social = await gather_social(client, p)
    await update.effective_message.reply_text(f"Writing ({kind})…")
    prompt = (
        f"{project_context(p, social)}\n\n"
        f"Write a ready-to-post {kind} piece for this project. "
        "Match a professional-but-human Web3 community voice. "
        "Do not invent product features. Include a light CTA."
    )
    out = await llm_write(prompt)
    await update.effective_message.reply_html(
        f"✍️ <b>WRITE · {esc(kind)} · {esc(p['name'])}</b>\n\n<code>{esc(out or 'AI offline')}</code>",
        disable_web_page_preview=True,
    )


async def cmd_thread(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await gate(update, context) or not update.effective_message:
        return
    args = context.args or []
    if len(args) < 2:
        await update.effective_message.reply_text("Usage: /thread <website|@x|CA> <topic>")
        return
    # first token id/name — rest topic. If id is multi-word name, user should use id.
    p = await resolve_project(update, context, args[0])
    topic = " ".join(args[1:])
    if not p:
        # try two-token name
        p = await resolve_project(update, context, " ".join(args[:2]))
        topic = " ".join(args[2:])
    if not p or not topic:
        await update.effective_message.reply_text("Need project + topic.")
        return
    _, client = deps(context)
    social = await gather_social(client, p)
    await update.effective_message.reply_text("Building thread…")
    prompt = (
        f"{project_context(p, social)}\n\n"
        f"Topic: {topic}\n\n"
        "Write a 7-tweet X thread: Hook, Problem, Explanation, Product, Example, Why it matters, CTA. "
        "Number them 1/7 … 7/7. Stay factual to the project context."
    )
    out = await llm_write(prompt)
    await update.effective_message.reply_html(
        f"🧵 <b>THREAD · {esc(p['name'])}</b>\nTopic: {esc(topic)}\n\n<code>{esc(out or 'AI offline')}</code>"
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
    if not context.args:
        await update.effective_message.reply_text("Usage: /contentgaps <website|@x|tg|CA>")
        return
    p = await resolve_project(update, context, " ".join(context.args))
    if not p:
        await update.effective_message.reply_text("Not found.")
        return
    _, client = deps(context)
    social = await gather_social(client, p)
    await update.effective_message.reply_text("Analyzing gaps…")
    prompt = (
        f"{project_context(p, social)}\n\n"
        "Identify CONTENT GAPS useful for a community/social operator pitching help:\n"
        "1) What the project seems to talk about\n"
        "2) What audience likely still asks (from thin docs / about text)\n"
        "3) Missing educational / onboarding / FAQ content\n"
        "4) 3 concrete post ideas that close those gaps\n"
        "Be honest when data is thin."
    )
    out = await llm_write(prompt)
    await update.effective_message.reply_html(
        f"🧩 <b>CONTENT GAPS · {esc(p['name'])}</b>\n\n{esc(out or 'AI offline')}"
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
    await q.answer()
    kind, _, rest = q.data.partition(":")
    if not rest.isdigit():
        return
    pid = int(rest)
    db, client = deps(context)
    p = await db.by_id(pid)
    if not p:
        await q.edit_message_text("Project not found.")
        return
    if kind == "p":
        context.args = [str(pid)]
        # reuse summary via message
        text = (
            f"📁 <b>#{p['id']} {esc(p['name'])}</b>\n"
            f"🌐 {esc(p.get('website') or '—')}\n𝕏 @{esc(p.get('x_handle') or '—')}\n"
            f"💬 {esc(p.get('telegram') or '—')}"
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🐦 Social", callback_data=f"soc:{pid}"),
                    InlineKeyboardButton("💡 Ideas", callback_data=f"id:{pid}"),
                ]
            ]
        )
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    elif kind == "soc":
        context.args = [str(pid)]
        await cmd_social(update, context)
    elif kind == "id":
        context.args = [str(pid)]
        await cmd_ideas(update, context)
    elif kind == "wr":
        context.args = [str(pid), "x"]
        await cmd_write(update, context)
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
    for name, fn in cmds:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
