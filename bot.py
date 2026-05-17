import asyncio
import html
import io
import json
import logging
import os
import random
import re
import secrets
import string
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
LINODE_API_TOKEN = os.getenv("LINODE_API_TOKEN", "").strip()
LINODE_ACCOUNT_NAME = os.getenv("LINODE_ACCOUNT_NAME", "default").strip() or "default"
LINODE_TOKENS_FILE = (
    os.getenv("LINODE_TOKENS_FILE", "tokens.json").strip() or "tokens.json"
)
ALLOWED_USER_IDS = {
    int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()
}
DEFAULT_REGION = os.getenv("DEFAULT_REGION", "ap-south")
DEFAULT_TYPE = os.getenv("DEFAULT_TYPE", "g6-standard-1")
DEFAULT_IMAGE = os.getenv("DEFAULT_IMAGE", "linode/ubuntu22.04")
MAX_COUNT = min(int(os.getenv("MAX_COUNT", "10")), 10)
AUTO_APPEND_CREATED_VPS = os.getenv("AUTO_APPEND_CREATED_VPS", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
GEN3_VPS_FILE = os.getenv("GEN3_VPS_FILE", "/root/work/gen3-vps/vps.json")
GEN3_VPS_USERNAME = os.getenv("GEN3_VPS_USERNAME", "root")
AUDIT_LOG_FILE = os.getenv(
    "AUDIT_LOG_FILE", os.path.join(BASE_DIR, "logs", "audit.jsonl")
)
EXPORT_DIR = os.getenv("EXPORT_DIR", os.path.join(BASE_DIR, "exports"))
HEALTH_TTL = int(os.getenv("HEALTH_TTL", "120"))
SMART_MAX_ATTEMPTS = int(os.getenv("SMART_MAX_ATTEMPTS", "12"))

API_BASE = "https://api.linode.com/v4"
CATALOG_TTL = 300
PAGE_SIZE = 8
DASH_PAGE_SIZE = 6

ACCOUNTS: dict[str, dict[str, Any]] = {}
ROUND_ROBIN_CURSOR = 0
PENDING_CREATES: dict[int, dict[str, Any]] = {}
PENDING_ACTIONS: dict[str, dict[str, Any]] = {}
PENDING_USER_ACTION: dict[int, str] = {}
PENDING_CONFIRM_TEXT: dict[int, str] = {}
PENDING_RESIZE_TARGETS: dict[int, list[dict[str, Any]]] = {}
WIZARDS: dict[int, dict[str, Any]] = {}
WIZARD_INPUT: dict[int, str] = {}
DASHBOARDS: dict[int, dict[str, Any]] = {}
LAST_CREATED: dict[int, list[dict[str, Any]]] = {}
CATALOG_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
ACCOUNT_HEALTH_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
log = logging.getLogger("linode-telegram-bot")


class BotError(Exception):
    pass


class LinodeAPIError(BotError):
    def __init__(self, status_code: int, detail: Any, account: str):
        self.status_code = status_code
        self.detail = detail
        self.account = account
        super().__init__(f"Linode API {status_code} [{account}]: {detail}")


def esc(x: Any) -> str:
    return html.escape(str(x), quote=False)


def short(x: Any, n: int = 36) -> str:
    s = str(x)
    return s if len(s) <= n else s[: n - 1] + "…"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_account_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip()).strip("-._")
    return name[:64] or "account"


def sanitize_group_name(name: str) -> str:
    return sanitize_account_name(name).lower()


def group_tag(name: str) -> str:
    return f"group:{sanitize_group_name(name)}"


def mask_token(token: str) -> str:
    token = token.strip()
    if len(token) <= 10:
        return "***"
    return f"{token[:6]}…{token[-4:]}"


def tokens_path() -> str:
    if os.path.isabs(LINODE_TOKENS_FILE):
        return LINODE_TOKENS_FILE
    return os.path.join(BASE_DIR, LINODE_TOKENS_FILE)


def load_accounts() -> dict[str, dict[str, Any]]:
    accounts: dict[str, dict[str, Any]] = {}
    path = tokens_path()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        rows = raw.get("accounts", []) if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            raise BotError("tokens.json harus list atau {accounts:[...]}")
        for idx, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                continue
            token = str(row.get("token") or "").strip()
            if not token:
                continue
            username = str(
                row.get("username") or row.get("name") or f"account-{idx}"
            ).strip()
            name = sanitize_account_name(
                str(row.get("name") or username or f"account-{idx}")
            )
            accounts[name] = {
                "name": name,
                "username": username,
                "token": token,
                "enabled": bool(row.get("enabled", True)),
            }
    if LINODE_API_TOKEN and not accounts:
        name = sanitize_account_name(LINODE_ACCOUNT_NAME)
        accounts[name] = {
            "name": name,
            "username": LINODE_ACCOUNT_NAME,
            "token": LINODE_API_TOKEN,
            "enabled": True,
        }
    return accounts


def save_accounts_to_file() -> None:
    path = tokens_path()
    rows = []
    for a in ACCOUNTS.values():
        rows.append(
            {
                "name": a["name"],
                "username": a.get("username", a["name"]),
                "token": a["token"],
                "enabled": a.get("enabled", True),
            }
        )
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)


async def validate_token(token: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    timeout = httpx.Timeout(30.0)
    result: dict[str, Any] = {"valid": False, "username": "", "email": "", "error": ""}
    try:
        async with httpx.AsyncClient(
            base_url=API_BASE, headers=headers, timeout=timeout
        ) as client:
            resp = await client.get("/profile")
        if resp.status_code == 200:
            data = resp.json()
            result["valid"] = True
            result["username"] = data.get("username", "")
            result["email"] = data.get("email", "")
        else:
            result["error"] = f"HTTP {resp.status_code}"
    except httpx.RequestError as e:
        result["error"] = str(e)
    return result


def reload_accounts() -> None:
    global ACCOUNTS
    ACCOUNTS = load_accounts()


def enabled_accounts() -> list[dict[str, Any]]:
    if not ACCOUNTS:
        reload_accounts()
    return [a for a in ACCOUNTS.values() if a.get("enabled") and a.get("token")]


def default_account_name() -> str:
    accounts = enabled_accounts()
    if not accounts:
        raise BotError(
            "No enabled Linode accounts. Isi tokens.json atau LINODE_API_TOKEN."
        )
    return accounts[0]["name"]


def get_account(name: str | None = None) -> dict[str, Any]:
    if not ACCOUNTS:
        reload_accounts()
    if not name:
        name = default_account_name()
    name = sanitize_account_name(name)
    account = ACCOUNTS.get(name)
    if not account or not account.get("enabled") or not account.get("token"):
        raise BotError(f"Account tidak valid/disabled: {name}")
    return account


def account_choice_text(obj: dict[str, Any]) -> str:
    mode = obj.get("account_mode", "specific")
    if mode == "random":
        return "🎲 RANDOM account per VPS"
    if mode == "roundrobin":
        return "🔁 ROUND-ROBIN accounts"
    if mode == "spread":
        return "🌐 SPREAD/ALL accounts"
    if mode == "smart":
        return "🧠 SMART best account"
    return str(obj.get("account") or default_account_name())


def parse_account_arg(
    value: str | None, fallback: dict[str, Any] | None = None
) -> tuple[str, str]:
    fallback = fallback or {}
    if not value:
        return (
            fallback.get("account_mode", "specific"),
            fallback.get("account") or default_account_name(),
        )
    v = value.strip()
    low = v.lower()
    if low in {"random", "rand", "acak"}:
        return "random", default_account_name()
    if low in {"roundrobin", "round-robin", "rr", "rotate"}:
        return "roundrobin", default_account_name()
    if low in {"all", "spread", "semua"}:
        return "spread", default_account_name()
    if low in {"smart", "auto", "pintar"}:
        return "smart", default_account_name()
    name = sanitize_account_name(v)
    get_account(name)
    return "specific", name


def parse_bool(value: str) -> bool:
    return str(value).lower() in {"1", "true", "yes", "y", "on"}


def clean_label(label: str) -> str:
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-._")
    return label[:58] or "linode-bot"


def gen_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in pwd)
            and any(c.isupper() for c in pwd)
            and any(c.isdigit() for c in pwd)
            and any(c in "!@#$%^&*()-_=+" for c in pwd)
        ):
            return pwd


def parse_kv(text: str, start: int = 1) -> dict[str, str]:
    parts = text.split()[start:]
    out: dict[str, str] = {}
    for part in parts:
        if "=" not in part:
            raise BotError(f"Arg invalid: {part}. Pakai key=value")
        k, v = part.split("=", 1)
        k = k.strip().lower()
        v = v.strip()
        if not k or not v:
            raise BotError(f"Arg invalid: {part}. Pakai key=value")
        out[k] = v
    return out


def user_info(update: Update | None) -> dict[str, Any]:
    if not update or not update.effective_user:
        return {}
    u = update.effective_user
    return {"id": u.id, "username": u.username, "name": u.full_name}


def redact(obj: Any) -> Any:
    sensitive = {"token", "root_pass", "password", "authorization", "linode_api_token"}
    if isinstance(obj, dict):
        return {
            k: ("***" if k.lower() in sensitive else redact(v)) for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


def audit_event(
    event: str,
    outcome: str = "info",
    update: Update | None = None,
    account: str | None = None,
    resource: dict[str, Any] | None = None,
    request: dict[str, Any] | None = None,
    error: Any = None,
    meta: dict[str, Any] | None = None,
) -> None:
    try:
        path = Path(AUDIT_LOG_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": now_iso(),
            "event": event,
            "outcome": outcome,
            "user": user_info(update),
            "account": account,
            "resource": resource or {},
            "request": redact(request or {}),
            "meta": meta or {},
            "error": str(error) if error else None,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception as e:
        log.warning("audit write failed: %s", e)


def is_allowed(user_id: int | None) -> bool:
    return bool(user_id and user_id in ALLOWED_USER_IDS)


async def guard(update: Update) -> bool:
    user = update.effective_user
    if not is_allowed(user.id if user else None):
        audit_event("auth.denied", "denied", update=update)
        msg = "Unauthorized. Jalankan /whoami lalu masukin ID lu ke ALLOWED_USER_IDS."
        if update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(msg)
        return False
    return True


async def linode_request(
    method: str, path: str, account: str | None = None, **kwargs: Any
) -> Any:
    acct = get_account(account)
    headers = {
        "Authorization": f"Bearer {acct['token']}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(45.0)
    try:
        async with httpx.AsyncClient(
            base_url=API_BASE, headers=headers, timeout=timeout
        ) as client:
            resp = await client.request(method, path, **kwargs)
    except httpx.RequestError as e:
        raise BotError(f"Linode network error [{acct['name']}]: {e}") from e
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise LinodeAPIError(resp.status_code, detail, acct["name"])
    if resp.status_code == 204 or not resp.content:
        return None
    return resp.json()


async def get_paginated(
    path: str, params: dict[str, Any] | None = None, account: str | None = None
) -> list[dict[str, Any]]:
    params = dict(params or {})
    params.setdefault("page_size", 100)
    page = 1
    data: list[dict[str, Any]] = []
    while True:
        params["page"] = page
        res = await linode_request("GET", path, account=account, params=params)
        data.extend(res.get("data", []))
        if page >= int(res.get("pages", 1)):
            return data
        page += 1


async def cached_catalog(
    key: str, path: str, params: dict[str, Any] | None = None, force: bool = False
) -> list[dict[str, Any]]:
    now = time.time()
    if not force and key in CATALOG_CACHE:
        ts, data = CATALOG_CACHE[key]
        if now - ts < CATALOG_TTL:
            return data
    data = await get_paginated(path, params, account=default_account_name())
    CATALOG_CACHE[key] = (now, data)
    return data


async def regions_catalog(force: bool = False) -> list[dict[str, Any]]:
    data = await cached_catalog("regions", "/regions", force=force)
    out = [r for r in data if "Linodes" in (r.get("capabilities") or [])]
    return sorted(out, key=lambda r: r.get("id", ""))


def monthly_price(t: dict[str, Any]) -> float:
    return float((t.get("price") or {}).get("monthly") or 0)


async def types_catalog(force: bool = False) -> list[dict[str, Any]]:
    data = await cached_catalog("types", "/linode/types", force=force)
    return sorted(data, key=lambda t: (monthly_price(t), t.get("id", "")))


async def images_catalog(force: bool = False) -> list[dict[str, Any]]:
    data = await cached_catalog("images", "/images", {"is_public": "true"}, force=force)
    images = [img for img in data if not img.get("deprecated")]
    popular = [
        "ubuntu24.04",
        "ubuntu22.04",
        "debian12",
        "debian11",
        "almalinux9",
        "rocky9",
        "centos-stream9",
    ]

    def key(img: dict[str, Any]) -> tuple[int, str]:
        iid = str(img.get("id", "")).lower()
        score = next((i for i, p in enumerate(popular) if p in iid), 99)
        return score, str(img.get("label") or iid)

    return sorted(images, key=key)


async def type_info(type_id: str) -> dict[str, Any] | None:
    for t in await types_catalog():
        if t.get("id") == type_id:
            return t
    return None


async def random_region_id() -> str:
    regions = await regions_catalog()
    if not regions:
        raise BotError("Region catalog kosong")
    return random.choice(regions)["id"]


async def region_pool(plan: dict[str, Any]) -> list[str]:
    if plan.get("random_region") or plan.get("region_mode") in {"random", "auto"}:
        regs = [r["id"] for r in await regions_catalog()]
        random.shuffle(regs)
        return regs
    return [plan["region"]]


async def account_health(account_name: str, force: bool = False) -> dict[str, Any]:
    account_name = get_account(account_name)["name"]
    now = time.time()
    if not force and account_name in ACCOUNT_HEALTH_CACHE:
        ts, data = ACCOUNT_HEALTH_CACHE[account_name]
        if now - ts < HEALTH_TTL:
            return data
    health = {
        "account": account_name,
        "ok": False,
        "status": "error",
        "token_ok": False,
        "can_create": False,
        "balance": None,
        "uninvoiced": None,
        "linodes_used": None,
        "linodes_limit": None,
        "linodes_remaining": None,
        "notifications": [],
        "last_error": "",
        "checked_at": now,
    }
    try:
        profile = await linode_request("GET", "/profile", account=account_name)
        acct = await linode_request("GET", "/account", account=account_name)
        health["token_ok"] = True
        health["username"] = profile.get("username")
        health["balance"] = acct.get("balance")
        health["uninvoiced"] = acct.get("balance_uninvoiced")
        try:
            limits = await linode_request(
                "GET", "/account/limits", account=account_name
            )
        except Exception as e:
            limits = {}
            health["last_error"] = f"limits unknown: {e}"
        instances = await get_paginated("/linode/instances", account=account_name)
        used = len(instances)
        limit = (
            limits.get("linodes")
            or limits.get("linode_instances")
            or limits.get("linode")
        )
        remaining = None if limit is None else max(int(limit) - used, 0)
        health["linodes_used"] = used
        health["linodes_limit"] = int(limit) if limit is not None else None
        health["linodes_remaining"] = remaining
        try:
            notif = await get_paginated("/account/notifications", account=account_name)
            health["notifications"] = [
                n.get("label") or n.get("type") for n in notif[:5]
            ]
        except Exception:
            pass
        blocked = bool(remaining is not None and remaining <= 0)
        health["ok"] = True
        health["can_create"] = not blocked
        health["status"] = "block" if blocked else ("warn" if limit is None else "ok")
    except LinodeAPIError as e:
        health["last_error"] = str(e)
        health["status"] = "block" if e.status_code in {401, 403} else "error"
    except Exception as e:
        health["last_error"] = str(e)
    ACCOUNT_HEALTH_CACHE[account_name] = (now, health)
    return health


async def accounts_health(
    scope: str = "all", force: bool = False
) -> list[dict[str, Any]]:
    if scope != "all":
        return [await account_health(scope, force=force)]
    return [await account_health(a["name"], force=force) for a in enabled_accounts()]


def health_icon(h: dict[str, Any]) -> str:
    return {"ok": "🟢", "warn": "🟡", "block": "🔴", "error": "🔴"}.get(
        h.get("status"), "⚪"
    )


async def health_text(scope: str = "all", force: bool = False) -> str:
    hs = await accounts_health(scope, force=force)
    lines = ["<b>Account Health / Quota</b>"]
    for h in hs:
        rem = "?" if h.get("linodes_remaining") is None else h.get("linodes_remaining")
        lim = "?" if h.get("linodes_limit") is None else h.get("linodes_limit")
        used = "?" if h.get("linodes_used") is None else h.get("linodes_used")
        bal = "?" if h.get("balance") is None else h.get("balance")
        note = (
            f" note=<code>{esc(h.get('last_error'))}</code>"
            if h.get("last_error")
            else ""
        )
        lines.append(
            f"{health_icon(h)} <code>{esc(h['account'])}</code> status=<b>{esc(h['status'])}</b> "
            f"quota=<code>{esc(rem)}/{esc(lim)}</code> used=<code>{esc(used)}</code> balance=<code>{esc(bal)}</code>{note}"
        )
    return "\n".join(lines)


def allocation_counts(accounts: list[dict[str, Any]]) -> Counter:
    return Counter(a["name"] for a in accounts)


def pick_accounts_for_plan(
    plan: dict[str, Any], healthy_accounts: list[str] | None = None
) -> list[dict[str, Any]]:
    global ROUND_ROBIN_CURSOR
    accounts = enabled_accounts()
    if healthy_accounts is not None:
        accounts = [a for a in accounts if a["name"] in healthy_accounts]
    if not accounts:
        raise BotError("No enabled accounts")
    count = int(plan["count"])
    mode = plan.get("account_mode", "specific")
    if mode == "random":
        return [random.choice(accounts) for _ in range(count)]
    if mode == "roundrobin":
        picked: list[dict[str, Any]] = []
        for _ in range(count):
            picked.append(accounts[ROUND_ROBIN_CURSOR % len(accounts)])
            ROUND_ROBIN_CURSOR += 1
        return picked
    if mode in {"spread", "smart"}:
        return [accounts[i % len(accounts)] for i in range(count)]
    return [get_account(plan.get("account")) for _ in range(count)]


async def preflight_create(plan: dict[str, Any], force: bool = False) -> dict[str, Any]:
    warnings: list[str] = []
    blockers: list[str] = []
    if plan.get("account_mode") == "smart":
        hs = await accounts_health("all", force=force)
        ok_names = [
            h["account"]
            for h in hs
            if h.get("can_create") or h.get("linodes_remaining") is None
        ]
        if not ok_names:
            blockers.append("Tidak ada account sehat untuk smart create")
        return {
            "ok": not blockers,
            "warnings": warnings,
            "blockers": blockers,
            "health": hs,
        }
    sequence = pick_accounts_for_plan(plan)
    counts = allocation_counts(sequence)
    hs = []
    for account_name, need in counts.items():
        h = await account_health(account_name, force=force)
        hs.append(h)
        remaining = h.get("linodes_remaining")
        if not h.get("token_ok"):
            blockers.append(f"{account_name}: token/account error")
        elif remaining is not None and remaining < need:
            blockers.append(
                f"{account_name}: quota kurang need={need} remaining={remaining}"
            )
        elif remaining is None:
            warnings.append(f"{account_name}: quota unknown, lanjut best-effort")
    return {
        "ok": not blockers,
        "warnings": warnings,
        "blockers": blockers,
        "health": hs,
    }


def preflight_summary(report: dict[str, Any]) -> str:
    lines: list[str] = []
    for b in report.get("blockers", []):
        lines.append(f"❌ {esc(b)}")
    for w in report.get("warnings", []):
        lines.append(f"⚠️ {esc(w)}")
    if not lines:
        lines.append("✅ quota preflight OK")
    return "\n".join(lines)


def default_wizard() -> dict[str, Any]:
    return {
        "account": default_account_name(),
        "account_mode": "specific",
        "region": DEFAULT_REGION,
        "region_mode": "specific",
        "random_region": False,
        "smart_create": False,
        "type": DEFAULT_TYPE,
        "image": DEFAULT_IMAGE,
        "label_prefix": "linode-bot",
        "count": 1,
        "root_pass": "",
        "tags": ["telegram-bot"],
        "group": "",
        "backups_enabled": False,
        "private_ip": False,
        "save_vps": AUTO_APPEND_CREATED_VPS,
    }


def get_wizard(user_id: int) -> dict[str, Any]:
    if user_id not in WIZARDS:
        WIZARDS[user_id] = default_wizard()
    return WIZARDS[user_id]


def build_plan_from_options(
    args: dict[str, str],
    user_id: int | None = None,
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    defaults = defaults or (get_wizard(user_id) if user_id else default_wizard())
    account_mode, account = parse_account_arg(
        args.get("account") or args.get("api"), defaults
    )
    count = int(args.get("count", defaults.get("count", 1)))
    if count < 1 or count > MAX_COUNT:
        raise BotError(f"count wajib 1-{MAX_COUNT}")
    region = args.get("region", defaults.get("region", DEFAULT_REGION))
    region_low = region.lower()
    random_region = parse_bool(
        args.get("random_region", str(defaults.get("random_region", False)))
    ) or region_low in {"random", "rand", "acak", "auto"}
    region_mode = (
        "auto" if region_low == "auto" else ("random" if random_region else "specific")
    )
    if random_region:
        region = "random"
    smart_create = (
        parse_bool(args.get("smart", str(defaults.get("smart_create", False))))
        or account_mode == "smart"
    )
    type_id = args.get("type", defaults.get("type", DEFAULT_TYPE))
    image = args.get("image", defaults.get("image", DEFAULT_IMAGE))
    label_prefix = clean_label(
        args.get(
            "label",
            args.get("label_prefix", defaults.get("label_prefix", "linode-bot")),
        )
    )
    root_pass = args.get("root_pass") or defaults.get("root_pass") or gen_password()
    tags = [
        x.strip()
        for x in args.get(
            "tags", ",".join(defaults.get("tags") or ["telegram-bot"])
        ).split(",")
        if x.strip()
    ]
    group = (
        sanitize_group_name(args.get("group", defaults.get("group", "")))
        if args.get("group", defaults.get("group", ""))
        else ""
    )
    if group:
        gt = group_tag(group)
        if gt not in tags:
            tags.append(gt)
    plan = {
        "account": account,
        "account_mode": account_mode,
        "region": region,
        "region_mode": region_mode,
        "random_region": random_region,
        "smart_create": smart_create,
        "type": type_id,
        "image": image,
        "label_prefix": label_prefix,
        "count": count,
        "root_pass": root_pass,
        "tags": tags,
        "group": group,
        "backups_enabled": parse_bool(
            args.get("backups", str(defaults.get("backups_enabled", False)))
        ),
        "private_ip": parse_bool(
            args.get("private_ip", str(defaults.get("private_ip", False)))
        ),
        "save_vps": parse_bool(
            args.get("save_vps", str(defaults.get("save_vps", AUTO_APPEND_CREATED_VPS)))
        ),
    }
    return plan


async def finalize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    info = await type_info(plan["type"])
    if not info:
        raise BotError(f"type tidak ketemu: {plan['type']}")
    plan["type_info"] = info
    plan["preflight"] = await preflight_create(plan)
    return plan


async def build_plan_from_args(update: Update) -> dict[str, Any]:
    args = parse_kv(update.effective_message.text or "")
    return await finalize_plan(build_plan_from_options(args, update.effective_user.id))


async def build_plan_from_wizard(user_id: int) -> dict[str, Any]:
    state = get_wizard(user_id)
    return await finalize_plan(build_plan_from_options({}, user_id, state))


def plan_summary(plan: dict[str, Any], include_secret: bool = False) -> str:
    info = plan["type_info"]
    price = info.get("price") or {}
    hourly = float(price.get("hourly") or 0) * plan["count"]
    monthly = float(price.get("monthly") or 0) * plan["count"]
    region_txt = "🎲 RANDOM per VPS" if plan.get("random_region") else plan["region"]
    lines = [
        "<b>Plan create Linode</b>",
        f"account: <code>{esc(account_choice_text(plan))}</code>",
        f"region: <code>{esc(region_txt)}</code>",
        f"smart: <code>{esc(plan.get('smart_create'))}</code>",
        f"type: <code>{esc(plan['type'])}</code>",
        f"image: <code>{esc(plan['image'])}</code>",
        f"label: <code>{esc(plan['label_prefix'])}-01..</code>",
        f"count: <b>{plan['count']}</b>",
        f"group: <code>{esc(plan.get('group') or '-')}</code>",
        f"backups: <code>{plan['backups_enabled']}</code>",
        f"private_ip: <code>{plan['private_ip']}</code>",
        f"save_vps_json: <code>{plan.get('save_vps')}</code>",
        f"tags: <code>{esc(','.join(plan['tags']))}</code>",
        f"est: <b>${hourly:.4f}/hour</b> | <b>${monthly:.2f}/month</b>",
    ]
    if plan.get("preflight"):
        lines.append("\n<b>Preflight</b>")
        lines.append(preflight_summary(plan["preflight"]))
    if include_secret:
        lines.append(f"root_pass: <code>{esc(plan['root_pass'])}</code>")
    else:
        lines.append("root_pass: <i>auto/custom hidden sampai create sukses</i>")
    return "\n".join(lines)


async def wizard_text(state: dict[str, Any]) -> str:
    info = await type_info(state["type"])
    price = info.get("price") if info else {}
    price_txt = f"${float(price.get('monthly') or 0):.2f}/mo" if price else "?"
    region_txt = "🎲 RANDOM per VPS" if state.get("random_region") else state["region"]
    root_txt = "custom" if state.get("root_pass") else "auto generated"
    return "\n".join(
        [
            "<b>🚀 Visual Linode Builder</b>",
            "Pilih via tombol. Catalog live dari Linode API.",
            "",
            f"👤 account: <code>{esc(account_choice_text(state))}</code>",
            f"🌍 region: <code>{esc(region_txt)}</code>",
            f"🧠 smart: <code>{esc(state.get('smart_create'))}</code>",
            f"📦 type: <code>{esc(state['type'])}</code> ({esc(price_txt)})",
            f"💿 image: <code>{esc(state['image'])}</code>",
            f"🔢 count: <b>{state['count']}</b>/<b>{MAX_COUNT}</b>",
            f"🏷 label: <code>{esc(state['label_prefix'])}</code>",
            f"👥 group: <code>{esc(state.get('group') or '-')}</code>",
            f"💾 backups: <code>{state['backups_enabled']}</code>",
            f"🔒 private_ip: <code>{state['private_ip']}</code>",
            f"📥 save_vps_json: <code>{state.get('save_vps')}</code>",
            f"🏷 tags: <code>{esc(','.join(state.get('tags') or []))}</code>",
            f"🔑 root_pass: <code>{esc(root_txt)}</code>",
        ]
    )


def wizard_keyboard(state: dict[str, Any]) -> InlineKeyboardMarkup:
    rr = "ON" if state.get("random_region") else "OFF"
    smart = "ON" if state.get("smart_create") else "OFF"
    backups = "ON" if state.get("backups_enabled") else "OFF"
    private = "ON" if state.get("private_ip") else "OFF"
    save = "ON" if state.get("save_vps") else "OFF"
    rows = [
        [
            InlineKeyboardButton("👤 Account", callback_data="wiz:pick:account:0"),
            InlineKeyboardButton("🌍 Region", callback_data="wiz:pick:region:0"),
        ],
        [
            InlineKeyboardButton(
                f"🎲 Random region {rr}", callback_data="wiz:toggle:random"
            ),
            InlineKeyboardButton(f"🧠 Smart {smart}", callback_data="wiz:toggle:smart"),
        ],
        [
            InlineKeyboardButton("📦 Plan/Type", callback_data="wiz:pick:type:0"),
            InlineKeyboardButton("💿 Image", callback_data="wiz:pick:image:0"),
        ],
        [
            InlineKeyboardButton("🔢 Count", callback_data="wiz:pick:count:0"),
            InlineKeyboardButton("🏷 Label", callback_data="wiz:ask:label"),
        ],
        [
            InlineKeyboardButton("👥 Group", callback_data="wiz:ask:group"),
            InlineKeyboardButton("🏷 Tags", callback_data="wiz:ask:tags"),
        ],
        [
            InlineKeyboardButton(
                f"💾 Backups {backups}", callback_data="wiz:toggle:backups"
            ),
            InlineKeyboardButton(
                f"🔒 Private IP {private}", callback_data="wiz:toggle:private"
            ),
        ],
        [
            InlineKeyboardButton(
                f"📥 Save JSON {save}", callback_data="wiz:toggle:save"
            ),
            InlineKeyboardButton("🔑 Root pass", callback_data="wiz:ask:root_pass"),
        ],
        [
            InlineKeyboardButton("🩺 Quota", callback_data="cmd:health"),
            InlineKeyboardButton("🔄 Scrape/Refresh", callback_data="wiz:refresh"),
        ],
        [
            InlineKeyboardButton("👀 Preview", callback_data="wiz:preview"),
            InlineKeyboardButton("✅ Build + Confirm", callback_data="wiz:build"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="wiz:cancel")],
    ]
    return InlineKeyboardMarkup(rows)


def nav_rows(kind: str, page: int, total: int) -> list[list[InlineKeyboardButton]]:
    last = max((total - 1) // PAGE_SIZE, 0)
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton("⬅️ Prev", callback_data=f"wiz:pick:{kind}:{page - 1}")
        )
    nav.append(InlineKeyboardButton("⬅️ Menu", callback_data="wiz:menu"))
    if page < last:
        nav.append(
            InlineKeyboardButton("Next ➡️", callback_data=f"wiz:pick:{kind}:{page + 1}")
        )
    return [nav]


async def picker_keyboard(kind: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    page = max(page, 0)
    if kind == "account":
        items = enabled_accounts()
        start = page * PAGE_SIZE
        shown = items[start : start + PAGE_SIZE]
        rows = [
            [
                InlineKeyboardButton(
                    "🧠 SMART BEST ACCOUNT", callback_data="wiz:set:account_mode:smart"
                )
            ],
            [
                InlineKeyboardButton(
                    "🌐 SPREAD/ALL ACCOUNTS",
                    callback_data="wiz:set:account_mode:spread",
                )
            ],
            [
                InlineKeyboardButton(
                    "🎲 RANDOM ACCOUNT PER VPS",
                    callback_data="wiz:set:account_mode:random",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔁 ROUND-ROBIN ACCOUNTS",
                    callback_data="wiz:set:account_mode:roundrobin",
                )
            ],
        ]
        for a in shown:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"👤 {a['name']} ({short(a.get('username'), 28)})",
                        callback_data=f"wiz:set:account:{a['name']}",
                    )
                ]
            )
        rows += nav_rows(kind, page, len(items))
        title = f"<b>👤 Pilih Account/API</b> page {page + 1}/{max((len(items) - 1) // PAGE_SIZE + 1, 1)}"
        return title, InlineKeyboardMarkup(rows)
    if kind == "region":
        items = await regions_catalog()
        start = page * PAGE_SIZE
        shown = items[start : start + PAGE_SIZE]
        rows = [
            [
                InlineKeyboardButton(
                    f"{r.get('id')} - {short(r.get('label'), 26)}",
                    callback_data=f"wiz:set:region:{r.get('id')}",
                )
            ]
            for r in shown
        ]
        rows.insert(
            0,
            [
                InlineKeyboardButton(
                    "🎲 RANDOM REGION PER VPS", callback_data="wiz:set:region:random"
                )
            ],
        )
        rows.insert(
            1,
            [
                InlineKeyboardButton(
                    "🧠 AUTO REGION SMART", callback_data="wiz:set:region:auto"
                )
            ],
        )
        title = f"<b>🌍 Pilih Region</b> page {page + 1}/{max((len(items) - 1) // PAGE_SIZE + 1, 1)}"
    elif kind == "type":
        items = await types_catalog()
        start = page * PAGE_SIZE
        shown = items[start : start + PAGE_SIZE]
        rows = []
        for t in shown:
            price = t.get("price") or {}
            txt = f"{t.get('id')} ${price.get('monthly', '-')}/mo"
            rows.append(
                [
                    InlineKeyboardButton(
                        short(txt, 55), callback_data=f"wiz:set:type:{t.get('id')}"
                    )
                ]
            )
        title = f"<b>📦 Pilih Plan/Type</b> page {page + 1}/{max((len(items) - 1) // PAGE_SIZE + 1, 1)}"
    elif kind == "image":
        items = await images_catalog()
        start = page * PAGE_SIZE
        shown = items[start : start + PAGE_SIZE]
        rows = []
        for img in shown:
            iid = img.get("id")
            label = img.get("label") or iid
            rows.append(
                [
                    InlineKeyboardButton(
                        short(f"{label} ({iid})", 55),
                        callback_data=f"wiz:set:image:{iid}",
                    )
                ]
            )
        title = f"<b>💿 Pilih Image</b> page {page + 1}/{max((len(items) - 1) // PAGE_SIZE + 1, 1)}"
    elif kind == "count":
        rows = []
        nums = list(range(1, MAX_COUNT + 1))
        for i in range(0, len(nums), 5):
            rows.append(
                [
                    InlineKeyboardButton(str(n), callback_data=f"wiz:set:count:{n}")
                    for n in nums[i : i + 5]
                ]
            )
        rows += nav_rows(kind, page, MAX_COUNT)
        return "<b>🔢 Pilih jumlah VPS</b>", InlineKeyboardMarkup(rows)
    else:
        raise BotError("Picker invalid")
    rows += nav_rows(kind, page, len(items))
    return title, InlineKeyboardMarkup(rows)


async def safe_edit_or_reply(
    update: Update, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    if update.callback_query:
        q = update.callback_query
        try:
            await q.edit_message_text(
                text, parse_mode="HTML", reply_markup=reply_markup
            )
        except Exception:
            await q.message.reply_html(text, reply_markup=reply_markup)
    else:
        await update.effective_message.reply_html(text, reply_markup=reply_markup)


async def render_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_wizard(update.effective_user.id)
    await safe_edit_or_reply(update, await wizard_text(state), wizard_keyboard(state))


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Confirm create", callback_data="confirm_create"),
                InlineKeyboardButton("Cancel", callback_data="cancel_create"),
            ]
        ]
    )


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🚀 Visual Create", callback_data="wiz:menu"),
                InlineKeyboardButton("📋 Dashboard", callback_data="dash:menu"),
            ],
            [
                InlineKeyboardButton("👤 Accounts", callback_data="cmd:accounts"),
                InlineKeyboardButton("🩺 Health/Quota", callback_data="cmd:health"),
            ],
            [
                InlineKeyboardButton("👥 Groups", callback_data="cmd:groups"),
                InlineKeyboardButton("📤 Export", callback_data="cmd:export"),
            ],
            [InlineKeyboardButton("🔄 Scrape Catalog", callback_data="cmd:refresh")],
            [
                InlineKeyboardButton("🌍 Regions", callback_data="cmd:regions"),
                InlineKeyboardButton("📦 Types", callback_data="cmd:types"),
            ],
            [InlineKeyboardButton("💿 Images", callback_data="cmd:images")],
        ]
    )


async def normalize_instance(
    x: dict[str, Any], account: str, root_pass: str | None = None
) -> dict[str, Any]:
    ipv4 = x.get("ipv4") or []
    if isinstance(ipv4, str):
        ipv4 = [ipv4]
    return {
        "account": account,
        "id": x.get("id"),
        "label": x.get("label"),
        "status": x.get("status"),
        "region": x.get("region"),
        "type": x.get("type"),
        "image": x.get("image"),
        "ipv4": ipv4,
        "ipv6": x.get("ipv6"),
        "tags": x.get("tags") or [],
        "created": x.get("created"),
        "updated": x.get("updated"),
        "username": GEN3_VPS_USERNAME,
        "password": root_pass,
    }


async def collect_instances(account_scope: str = "all") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    accounts = (
        enabled_accounts() if account_scope == "all" else [get_account(account_scope)]
    )
    for acct in accounts:
        try:
            instances = await get_paginated("/linode/instances", account=acct["name"])
            for x in instances:
                rows.append(await normalize_instance(x, acct["name"]))
        except Exception as e:
            rows.append(
                {
                    "account": acct["name"],
                    "id": None,
                    "label": f"ERROR: {e}",
                    "status": "error",
                    "ipv4": [],
                    "tags": [],
                }
            )
    return rows


def first_ip(row: dict[str, Any]) -> str:
    ips = row.get("ipv4") or []
    return ips[0] if ips else "-"


def format_rows_short(rows: list[dict[str, Any]], title: str) -> str:
    if not rows:
        return f"<b>{esc(title)}</b>\nNo VPS."
    lines = [f"<b>{esc(title)}</b>"]
    for x in rows[:80]:
        lines.append(
            f"<code>{esc(x.get('account'))}</code> <code>{esc(x.get('id'))}</code> "
            f"<b>{esc(x.get('label'))}</b> {esc(x.get('region'))}/{esc(x.get('type'))} "
            f"{esc(x.get('status'))} ip=<code>{esc(first_ip(x))}</code>"
        )
    return "\n".join(lines)


def default_dashboard() -> dict[str, Any]:
    return {
        "account": "all",
        "region": "all",
        "status": "all",
        "search": "",
        "page": 0,
        "selected": set(),
        "page_items": [],
    }


def get_dashboard(uid: int) -> dict[str, Any]:
    if uid not in DASHBOARDS:
        DASHBOARDS[uid] = default_dashboard()
    return DASHBOARDS[uid]


def row_key(row: dict[str, Any]) -> str:
    return f"{row.get('account')}:{row.get('id')}"


def filter_dashboard_rows(
    rows: list[dict[str, Any]], state: dict[str, Any]
) -> list[dict[str, Any]]:
    out = []
    q = str(state.get("search") or "").lower()
    for r in rows:
        if not r.get("id"):
            continue
        if state.get("region") not in {None, "all"} and r.get("region") != state.get(
            "region"
        ):
            continue
        if state.get("status") not in {None, "all"} and r.get("status") != state.get(
            "status"
        ):
            continue
        if (
            q
            and q not in str(r.get("label", "")).lower()
            and q not in str(r.get("id", "")).lower()
            and q not in first_ip(r)
        ):
            continue
        out.append(r)
    return out


async def render_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    state = get_dashboard(uid)
    rows = filter_dashboard_rows(
        await collect_instances(state.get("account", "all")), state
    )
    state["all_filtered"] = rows
    total = len(rows)
    last = max((total - 1) // DASH_PAGE_SIZE, 0)
    state["page"] = max(0, min(int(state.get("page", 0)), last))
    start = state["page"] * DASH_PAGE_SIZE
    shown = rows[start : start + DASH_PAGE_SIZE]
    state["page_items"] = shown
    selected = state.get("selected") or set()
    lines = [
        "<b>📋 VPS Dashboard</b>",
        f"account=<code>{esc(state.get('account'))}</code> region=<code>{esc(state.get('region'))}</code> status=<code>{esc(state.get('status'))}</code> search=<code>{esc(state.get('search') or '-')}</code>",
        f"shown={len(shown)} total={total} selected={len(selected)} page={state['page'] + 1}/{last + 1}",
    ]
    for i, r in enumerate(shown):
        mark = "✅" if row_key(r) in selected else "⬜"
        lines.append(
            f"{mark} {i+1}. <code>{esc(r['account'])}</code> <code>{esc(r['id'])}</code> <b>{esc(r['label'])}</b> {esc(r['status'])} {esc(first_ip(r))}"
        )
    rows_btn: list[list[InlineKeyboardButton]] = []
    for i, r in enumerate(shown):
        mark = "✅" if row_key(r) in selected else "⬜"
        rows_btn.append(
            [
                InlineKeyboardButton(
                    f"{mark} {short(r.get('label'), 18)}",
                    callback_data=f"dash:toggle:{i}",
                ),
                InlineKeyboardButton("🗑", callback_data=f"dash:del:{i}"),
                InlineKeyboardButton("🔁", callback_data=f"dash:reboot:{i}"),
            ]
        )
    nav = []
    if state["page"] > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data="dash:prev"))
    nav.append(InlineKeyboardButton("🔄", callback_data="dash:menu"))
    if state["page"] < last:
        nav.append(InlineKeyboardButton("➡️", callback_data="dash:next"))
    rows_btn.append(nav)
    rows_btn += [
        [
            InlineKeyboardButton("👤 Account", callback_data="dash:filter:account"),
            InlineKeyboardButton("🌍 Region", callback_data="dash:ask:region"),
            InlineKeyboardButton("🟢 Status", callback_data="dash:filter:status"),
        ],
        [
            InlineKeyboardButton("🔎 Search", callback_data="dash:ask:search"),
            InlineKeyboardButton("☑️ Select page", callback_data="dash:selectpage"),
            InlineKeyboardButton("🧹 Clear", callback_data="dash:clear"),
        ],
        [
            InlineKeyboardButton("🗑 Delete selected", callback_data="dash:bulk:delete"),
            InlineKeyboardButton(
                "🔁 Reboot selected", callback_data="dash:bulk:reboot"
            ),
        ],
        [
            InlineKeyboardButton(
                "📦 Resize selected", callback_data="dash:bulk:resize"
            ),
            InlineKeyboardButton(
                "📤 Export selected", callback_data="dash:bulk:export"
            ),
        ],
        [
            InlineKeyboardButton(
                "☢️ Delete ALL filtered", callback_data="dash:all:delete"
            ),
            InlineKeyboardButton("⬅️ Main", callback_data="cmd:start"),
        ],
    ]
    await safe_edit_or_reply(update, "\n".join(lines), InlineKeyboardMarkup(rows_btn))


def make_targets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "account": r["account"],
            "id": int(r["id"]),
            "label": r.get("label"),
            "region": r.get("region"),
            "type": r.get("type"),
            "ipv4": r.get("ipv4") or [],
        }
        for r in rows
        if r.get("id")
    ]


def create_pending_action(
    uid: int,
    action: str,
    targets: list[dict[str, Any]],
    meta: dict[str, Any] | None = None,
) -> str:
    action_id = uuid.uuid4().hex[:10]
    PENDING_ACTIONS[action_id] = {
        "uid": uid,
        "action": action,
        "targets": targets,
        "meta": meta or {},
        "created_at": time.time(),
    }
    PENDING_USER_ACTION[uid] = action_id
    return action_id


def action_confirm_phrase(action: str, count: int) -> str | None:
    if count <= 1:
        return None
    return {"delete": "DELETE", "reboot": "REBOOT", "resize": "RESIZE"}.get(action)


def pending_action_text(action_id: str) -> str:
    p = PENDING_ACTIONS[action_id]
    action = p["action"]
    targets = p["targets"]
    lines = [f"<b>Confirm {esc(action)}</b>", f"targets: <b>{len(targets)}</b>"]
    if p.get("meta", {}).get("type"):
        lines.append(f"new_type: <code>{esc(p['meta']['type'])}</code>")
    for t in targets[:20]:
        lines.append(
            f"- <code>{esc(t['account'])}</code> <code>{esc(t['id'])}</code> {esc(t.get('label'))}"
        )
    if len(targets) > 20:
        lines.append(f"... +{len(targets)-20} lagi")
    phrase = action_confirm_phrase(action, len(targets))
    if phrase:
        lines.append(f"\nKetik <code>{phrase}</code> untuk lanjut.")
    return "\n".join(lines)


def pending_action_keyboard(action_id: str) -> InlineKeyboardMarkup:
    p = PENDING_ACTIONS[action_id]
    phrase = action_confirm_phrase(p["action"], len(p["targets"]))
    if phrase:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("Cancel", callback_data=f"act:cancel:{action_id}")]]
        )
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Confirm", callback_data=f"act:confirm:{action_id}"
                ),
                InlineKeyboardButton("Cancel", callback_data=f"act:cancel:{action_id}"),
            ]
        ]
    )


async def execute_pending_action(
    uid: int, action_id: str, update: Update | None = None, message=None
) -> None:
    p = PENDING_ACTIONS.pop(action_id, None)
    PENDING_USER_ACTION.pop(uid, None)
    PENDING_CONFIRM_TEXT.pop(uid, None)
    target_msg = message or (update.effective_message if update else None)
    if not p:
        if target_msg:
            await target_msg.reply_text("Pending action tidak ketemu/expired.")
        return
    action = p["action"]
    ok: list[str] = []
    err: list[str] = []
    audit_event(
        f"linode.{action}.confirmed",
        "confirmed",
        update=update,
        request={"targets": p["targets"], "meta": p.get("meta")},
    )
    for t in p["targets"]:
        try:
            if action == "delete":
                await linode_request(
                    "DELETE", f"/linode/instances/{t['id']}", account=t["account"]
                )
            elif action == "reboot":
                await linode_request(
                    "POST", f"/linode/instances/{t['id']}/reboot", account=t["account"]
                )
            elif action == "resize":
                new_type = p.get("meta", {}).get("type")
                if not new_type:
                    raise BotError("resize type kosong")
                await linode_request(
                    "POST",
                    f"/linode/instances/{t['id']}/resize",
                    account=t["account"],
                    json={"type": new_type, "allow_auto_disk_resize": True},
                )
            else:
                raise BotError(f"action invalid: {action}")
            ok.append(f"✅ {t['account']}:{t['id']} {t.get('label')}")
            audit_event(
                f"linode.{action}.success",
                "success",
                update=update,
                account=t["account"],
                resource={"type": "linode", "id": t["id"], "label": t.get("label")},
            )
        except Exception as e:
            err.append(f"❌ {t['account']}:{t['id']} {e}")
            audit_event(
                f"linode.{action}.failed",
                "failed",
                update=update,
                account=t.get("account"),
                resource={"type": "linode", "id": t.get("id"), "label": t.get("label")},
                error=e,
            )
    lines = [f"<b>{esc(action)} result</b>"] + ok + err
    if target_msg:
        await target_msg.reply_html("\n".join(lines))


def classify_linode_error(e: Exception) -> str:
    txt = str(e).lower()
    if isinstance(e, LinodeAPIError):
        if e.status_code in {429, 500, 502, 503, 504}:
            return "transient"
        if e.status_code in {401, 403, 402}:
            return "account"
        if any(x in txt for x in ["quota", "limit", "payment", "credit", "permission"]):
            return "account"
        if any(x in txt for x in ["region", "capacity", "available", "availability"]):
            return "region"
        if e.status_code in {400, 404, 422}:
            return "fatal"
    if any(x in txt for x in ["timeout", "network", "temporar"]):
        return "transient"
    return "fatal"


async def create_linode_once(
    plan: dict[str, Any], index: int, account: str, region: str
) -> dict[str, Any]:
    suffix = f"-{index:02d}" if plan["count"] > 1 else ""
    label = clean_label(f"{plan['label_prefix']}{suffix}")
    payload = {
        "region": region,
        "type": plan["type"],
        "image": plan["image"],
        "label": label,
        "root_pass": plan["root_pass"],
        "tags": plan["tags"],
        "backups_enabled": plan["backups_enabled"],
        "private_ip": plan["private_ip"],
        "booted": True,
    }
    res = await linode_request(
        "POST", "/linode/instances", account=account, json=payload
    )
    res["_chosen_region"] = region
    res["_account"] = account
    return res


async def smart_create_one(
    plan: dict[str, Any], index: int
) -> tuple[dict[str, Any] | None, list[str]]:
    attempts: list[str] = []
    hs = await accounts_health("all")
    healthy = [
        h["account"]
        for h in hs
        if h.get("can_create") or h.get("linodes_remaining") is None
    ]
    acct_pool = healthy or [a["name"] for a in enabled_accounts()]
    regs = await region_pool(plan)
    if not regs:
        regs = [DEFAULT_REGION]
    random.shuffle(acct_pool)
    random.shuffle(regs)
    tried = 0
    for acct in list(acct_pool):
        for region in list(regs):
            if tried >= SMART_MAX_ATTEMPTS:
                return None, attempts
            tried += 1
            try:
                res = await create_linode_once(plan, index, acct, region)
                attempts.append(f"✅ {acct}/{region}")
                return res, attempts
            except Exception as e:
                kind = classify_linode_error(e)
                attempts.append(f"❌ {acct}/{region} {kind}: {e}")
                if kind == "transient":
                    await asyncio.sleep(min(tried, 3))
                    continue
                if kind == "fatal":
                    return None, attempts
                continue
    return None, attempts


async def fetch_instance_until_ip(
    account: str, linode_id: int, tries: int = 3
) -> dict[str, Any] | None:
    for _ in range(tries):
        try:
            x = await linode_request(
                "GET", f"/linode/instances/{linode_id}", account=account
            )
            if x.get("ipv4"):
                return x
        except Exception:
            pass
        await asyncio.sleep(2)
    return None


def append_to_vps_json(rows: list[dict[str, Any]]) -> dict[str, Any]:
    path = Path(GEN3_VPS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    data: list[dict[str, Any]] = []
    if path.exists() and path.stat().st_size:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, list):
            raise BotError("vps.json bukan JSON array")
        data = loaded
    seen = {x.get("host") for x in data if isinstance(x, dict)}
    added = 0
    skipped = 0
    for r in rows:
        password = r.get("password")
        for ip in r.get("ipv4") or []:
            if not ip or ip in seen:
                skipped += 1
                continue
            data.append(
                {"host": ip, "username": GEN3_VPS_USERNAME, "password": password}
            )
            seen.add(ip)
            added += 1
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return {"path": str(path), "total": len(data), "added": added, "skipped": skipped}


async def do_create(user_id: int, update: Update | None = None, message=None) -> None:
    plan = PENDING_CREATES.pop(user_id, None)
    target = message or (update.effective_message if update else None)
    if not plan:
        if target:
            await target.reply_text("Tidak ada pending create. Pakai /create dulu.")
        return
    report = await preflight_create(plan, force=True)
    if not report.get("ok") and not plan.get("smart_create"):
        if target:
            await target.reply_html(
                "<b>Create diblock preflight</b>\n" + preflight_summary(report)
            )
        PENDING_CREATES[user_id] = plan
        return
    created_raw: list[dict[str, Any]] = []
    created_norm: list[dict[str, Any]] = []
    errors: list[str] = []
    audit_event("linode.create.confirmed", "confirmed", update=update, request=plan)
    if plan.get("smart_create") or plan.get("account_mode") == "smart":
        for i in range(1, plan["count"] + 1):
            res, attempts = await smart_create_one(plan, i)
            if res:
                created_raw.append(res)
            else:
                errors.append(
                    f"smart-{i:02d}: gagal setelah attempts: {' | '.join(attempts[-5:])}"
                )
    else:
        accounts = pick_accounts_for_plan(plan)
        regs = await region_pool(plan)
        for i in range(1, plan["count"] + 1):
            account = accounts[i - 1]["name"]
            region = (
                random.choice(regs) if plan.get("random_region") else plan["region"]
            )
            try:
                res = await create_linode_once(plan, i, account, region)
                created_raw.append(res)
                await asyncio.sleep(0.7)
            except Exception as e:
                errors.append(f"{plan['label_prefix']}-{i:02d}@{region}/{account}: {e}")
                audit_event(
                    "linode.create.failed",
                    "failed",
                    update=update,
                    account=account,
                    request={"index": i, "plan": plan},
                    error=e,
                )
    for x in created_raw:
        account = x.get("_account") or plan.get("account")
        if not x.get("ipv4") and x.get("id"):
            fresh = await fetch_instance_until_ip(account, int(x["id"]))
            if fresh:
                fresh["_account"] = account
                fresh["_chosen_region"] = x.get("_chosen_region")
                x = fresh
        norm = await normalize_instance(x, account, root_pass=plan["root_pass"])
        created_norm.append(norm)
        audit_event(
            "linode.create.success",
            "success",
            update=update,
            account=account,
            resource={"type": "linode", "id": x.get("id"), "label": x.get("label")},
            request=plan,
        )
    LAST_CREATED[user_id] = created_norm
    lines = ["<b>Create result</b>"]
    for x in created_norm:
        lines.append(
            f"✅ <code>{esc(x.get('label'))}</code> id=<code>{esc(x.get('id'))}</code> "
            f"account=<code>{esc(x.get('account'))}</code> region=<code>{esc(x.get('region'))}</code> ip=<code>{esc(first_ip(x))}</code>"
        )
    save_info = None
    if created_norm and plan.get("save_vps"):
        try:
            save_info = append_to_vps_json(created_norm)
            lines.append(
                f"vps.json: added={save_info['added']} skipped={save_info['skipped']} path=<code>{esc(save_info['path'])}</code>"
            )
            audit_event(
                "vpsjson.append.success", "success", update=update, request=save_info
            )
        except Exception as e:
            lines.append(f"vps.json error: <code>{esc(e)}</code>")
            audit_event("vpsjson.append.failed", "failed", update=update, error=e)
    if created_norm:
        lines.append(f"root_pass: <code>{esc(plan['root_pass'])}</code>")
        lines.append(
            "Simpan sekarang. Password tidak disimpan kecuali save_vps_json ON."
        )
    for err in errors:
        lines.append(f"❌ {esc(err)}")
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📤 Export new JSON", callback_data="export:new:json"
                ),
                InlineKeyboardButton(
                    "📄 Export new TXT", callback_data="export:new:txt"
                ),
            ],
            [InlineKeyboardButton("📋 Dashboard", callback_data="dash:menu")],
        ]
    )
    if target:
        await target.reply_html("\n".join(lines), reply_markup=kb)


def export_rows_json(rows: list[dict[str, Any]], include_secret: bool = False) -> str:
    data = (
        rows
        if include_secret
        else [{k: v for k, v in r.items() if k != "password"} for r in rows]
    )
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def export_rows_txt(
    rows: list[dict[str, Any]], include_secret: bool = False, wa: bool = False
) -> str:
    lines: list[str] = []
    for r in rows:
        pwd = r.get("password") if include_secret else None
        if wa:
            lines.append(
                f"✅ VPS {r.get('label')}\nIP: {first_ip(r)}\nUser: {r.get('username', 'root')}\nPass: {pwd or '-'}\nAccount: {r.get('account')}\nRegion: {r.get('region')}\nType: {r.get('type')}"
            )
        else:
            lines.append(
                f"{r.get('label')} | {first_ip(r)} | {r.get('username','root')} | {pwd or '-'} | {r.get('account')} | {r.get('region')} | {r.get('type')} | {r.get('status')}"
            )
    return "\n".join(lines) + ("\n" if lines else "")


def export_rows(
    rows: list[dict[str, Any]], fmt: str, include_secret: bool = False
) -> tuple[str, str]:
    fmt = fmt.lower()
    if fmt == "json":
        return "json", export_rows_json(rows, include_secret)
    if fmt in {"wa", "whatsapp"}:
        return "txt", export_rows_txt(rows, include_secret, wa=True)
    return "txt", export_rows_txt(rows, include_secret)


async def send_export(
    target_message,
    rows: list[dict[str, Any]],
    fmt: str = "txt",
    title: str = "vps-export",
    include_secret: bool = False,
) -> None:
    ext, content = export_rows(rows, fmt, include_secret)
    if not rows:
        await target_message.reply_text("No rows to export.")
        return
    if fmt in {"wa", "tg", "telegram"} and len(content) < 3900:
        await target_message.reply_text(content)
        return
    Path(EXPORT_DIR).mkdir(parents=True, exist_ok=True)
    filename = f"{clean_label(title)}-{int(time.time())}.{ext}"
    path = os.path.join(EXPORT_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    with open(path, "rb") as f:
        await target_message.reply_document(
            document=f, filename=filename, caption=f"Export {len(rows)} VPS"
        )
    audit_event(
        "vps.export.success",
        "success",
        request={
            "title": title,
            "fmt": fmt,
            "rows": len(rows),
            "include_secret": include_secret,
        },
    )


async def list_text(uid: int | None = None, account_name: str | None = None) -> str:
    rows = await collect_instances(account_name or "all")
    return format_rows_short(rows, f"Linodes - {account_name or 'all'}")


def list_account_from_args(uid: int, text: str) -> str:
    parts = text.split()
    if len(parts) <= 1:
        state = get_wizard(uid)
        if state.get("account_mode") == "specific":
            return state.get("account") or default_account_name()
        return "all"
    arg = parts[1]
    if arg.lower() == "all":
        return "all"
    if arg.startswith("account="):
        arg = arg.split("=", 1)[1]
    name = sanitize_account_name(arg)
    get_account(name)
    return name


async def accounts_text(uid: int | None = None) -> str:
    reload_accounts()
    lines = ["<b>Linode Accounts</b>"]
    if uid is not None:
        try:
            lines.append(
                f"active: <code>{esc(account_choice_text(get_wizard(uid)))}</code>"
            )
        except Exception:
            pass
    if not ACCOUNTS:
        lines.append("No accounts.")
    for a in ACCOUNTS.values():
        status = "ON" if a.get("enabled") else "OFF"
        lines.append(
            f"<code>{esc(a['name'])}</code> [{status}] user=<code>{esc(a.get('username', '-'))}</code> token=<code>{esc(mask_token(a.get('token', '')))}</code>"
        )
    lines.append(
        "\nSet: <code>/account NAME</code> | <code>/account random</code> | <code>/account roundrobin</code> | <code>/account spread</code> | <code>/account smart</code>"
    )
    return "\n".join(lines)


async def collect_group_targets(name: str, scope: str = "all") -> list[dict[str, Any]]:
    tag = group_tag(name)
    rows = await collect_instances(scope)
    return [r for r in rows if tag in (r.get("tags") or [])]


async def groups_text(scope: str = "all") -> str:
    rows = await collect_instances(scope)
    counts: Counter = Counter()
    for r in rows:
        for t in r.get("tags") or []:
            if str(t).startswith("group:"):
                counts[str(t)[6:]] += 1
    lines = [f"<b>Groups - {esc(scope)}</b>"]
    if not counts:
        lines.append("No groups.")
    for name, count in sorted(counts.items()):
        lines.append(f"<code>{esc(name)}</code>: <b>{count}</b> VPS")
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await update.message.reply_html(
        "<b>Linode bot ready.</b>\n\n"
        "Visual: /create /wizard /dashboard\n"
        "Smart CLI:\n"
        "<code>/create account=smart region=auto smart=true type=g6-standard-1 image=linode/ubuntu22.04 label=test count=3</code>\n\n"
        "Cmd: /accounts /token /quota /dashboard /groups /export /list /delete /regions /types /images /refresh /whoami",
        reply_markup=main_keyboard(),
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_text(f"user_id={user.id if user else 'unknown'}")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await update.message.reply_html(
        "<b>Usage</b>\n"
        "Visual: <code>/wizard</code> / <code>/dashboard</code>\n"
        "Create: <code>/create account=smart region=auto smart=true type=g6-standard-1 image=linode/ubuntu22.04 label=web count=2 group=prod</code>\n"
        "Multi: <code>account=random|roundrobin|spread|smart</code>\n"
        "Delete visual: <code>/dashboard</code> lalu select. Mass/all wajib ketik DELETE.\n"
        "Token: <code>/token add|list|validate|delete|enable|disable</code>\n"
        "Quota: <code>/quota all</code>\n"
        "Export: <code>/export all json</code> / <code>/export_new txt</code>\n"
        "Group: <code>/group create web count=2</code> / <code>/group delete web all</code>"
    )


async def wizard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await render_wizard(update, context)


async def dashboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await render_dashboard(update, context)


async def token_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    parts = (update.message.text or "").split(maxsplit=2)
    sub = parts[1].lower() if len(parts) > 1 else ""

    if sub == "list":
        reload_accounts()
        if not ACCOUNTS:
            await update.message.reply_text("No tokens configured.")
            return
        lines = ["<b>Token List</b>"]
        for i, a in enumerate(ACCOUNTS.values(), 1):
            status = "ON" if a.get("enabled") else "OFF"
            lines.append(
                f"{i}. <code>{esc(a['name'])}</code> [{status}] "
                f"user=<code>{esc(a.get('username', '-'))}</code> "
                f"token=<code>{esc(mask_token(a.get('token', '')))}</code>"
            )
        await update.message.reply_html("\n".join(lines))

    elif sub == "add":
        if len(parts) < 3:
            await update.message.reply_html(
                "Usage: <code>/token add TOKEN</code>\n"
                "atau: <code>/token add TOKEN name=NAMA</code>"
            )
            return
        rest = parts[2].strip().split()
        token_val = rest[0]
        name_arg = ""
        for r in rest[1:]:
            if r.startswith("name="):
                name_arg = r.split("=", 1)[1]
        msg = await update.message.reply_text("Validating token...")
        result = await validate_token(token_val)
        if not result["valid"]:
            await msg.edit_text(f"Token INVALID: {result['error']}")
            audit_event(
                "token.add.failed", "error", update=update, error=result["error"]
            )
            return
        username = result["username"]
        name = sanitize_account_name(name_arg or username)
        if name in ACCOUNTS:
            await msg.edit_text(
                f"Account '{name}' sudah ada. Hapus dulu atau pakai name= berbeda."
            )
            return
        ACCOUNTS[name] = {
            "name": name,
            "username": username,
            "token": token_val,
            "enabled": True,
        }
        save_accounts_to_file()
        audit_event("token.add", "success", update=update, account=name)
        await msg.edit_text(
            f"Token VALID & ditambahkan.\n"
            f"Name: {name}\nUsername: {username}\nEmail: {result['email']}"
        )

    elif sub == "delete" or sub == "remove":
        if len(parts) < 3:
            await update.message.reply_html("Usage: <code>/token delete NAMA</code>")
            return
        name = sanitize_account_name(parts[2].strip())
        reload_accounts()
        if name not in ACCOUNTS:
            await update.message.reply_text(f"Account '{name}' tidak ditemukan.")
            return
        del ACCOUNTS[name]
        save_accounts_to_file()
        audit_event("token.delete", "success", update=update, account=name)
        await update.message.reply_text(f"Token '{name}' dihapus.")

    elif sub == "validate":
        reload_accounts()
        if len(parts) >= 3:
            name = sanitize_account_name(parts[2].strip())
            if name not in ACCOUNTS:
                await update.message.reply_text(f"Account '{name}' tidak ditemukan.")
                return
            targets = {name: ACCOUNTS[name]}
        else:
            targets = dict(ACCOUNTS)
        if not targets:
            await update.message.reply_text("No tokens to validate.")
            return
        msg = await update.message.reply_text(f"Validating {len(targets)} token(s)...")
        lines = ["<b>Token Validation</b>"]
        invalid_names = []
        for aname, acct in targets.items():
            result = await validate_token(acct["token"])
            if result["valid"]:
                lines.append(f"<code>{esc(aname)}</code>: VALID ({result['username']})")
            else:
                lines.append(f"<code>{esc(aname)}</code>: INVALID ({result['error']})")
                invalid_names.append(aname)
        if invalid_names:
            lines.append(f"\n{len(invalid_names)} invalid. Hapus dengan:")
            for n in invalid_names:
                lines.append(f"<code>/token delete {esc(n)}</code>")
        else:
            lines.append(f"\nSemua {len(targets)} token valid.")
        await msg.edit_text("\n".join(lines), parse_mode="HTML")

    elif sub == "enable":
        if len(parts) < 3:
            await update.message.reply_html("Usage: <code>/token enable NAMA</code>")
            return
        name = sanitize_account_name(parts[2].strip())
        reload_accounts()
        if name not in ACCOUNTS:
            await update.message.reply_text(f"Account '{name}' tidak ditemukan.")
            return
        ACCOUNTS[name]["enabled"] = True
        save_accounts_to_file()
        await update.message.reply_text(f"Token '{name}' enabled.")

    elif sub == "disable":
        if len(parts) < 3:
            await update.message.reply_html("Usage: <code>/token disable NAMA</code>")
            return
        name = sanitize_account_name(parts[2].strip())
        reload_accounts()
        if name not in ACCOUNTS:
            await update.message.reply_text(f"Account '{name}' tidak ditemukan.")
            return
        ACCOUNTS[name]["enabled"] = False
        save_accounts_to_file()
        await update.message.reply_text(f"Token '{name}' disabled.")

    else:
        await update.message.reply_html(
            "<b>Token Management</b>\n\n"
            "<code>/token list</code> — lihat semua token\n"
            "<code>/token add TOKEN [name=NAMA]</code> — tambah & validasi token\n"
            "<code>/token delete NAMA</code> — hapus token\n"
            "<code>/token validate [NAMA]</code> — cek token valid/invalid\n"
            "<code>/token enable NAMA</code> — aktifkan token\n"
            "<code>/token disable NAMA</code> — nonaktifkan token"
        )


async def accounts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await update.message.reply_html(
        await accounts_text(update.effective_user.id),
        reply_markup=(await picker_keyboard("account", 0))[1],
    )


async def account_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_html(
            await accounts_text(update.effective_user.id),
            reply_markup=(await picker_keyboard("account", 0))[1],
        )
        return
    try:
        mode, account = parse_account_arg(
            parts[1], get_wizard(update.effective_user.id)
        )
        state = get_wizard(update.effective_user.id)
        state["account_mode"] = mode
        state["account"] = account
        audit_event(
            "account.selected",
            "success",
            update=update,
            account=account,
            request={"mode": mode},
        )
        await update.message.reply_html(
            f"Active account: <code>{esc(account_choice_text(state))}</code>"
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def create_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    text = update.effective_message.text or ""
    if len(text.split()) <= 1:
        await render_wizard(update, context)
        return
    try:
        plan = await build_plan_from_args(update)
        PENDING_CREATES[update.effective_user.id] = plan
        audit_event("linode.create.requested", "requested", update=update, request=plan)
        await update.message.reply_html(
            plan_summary(plan), reply_markup=confirm_keyboard()
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def confirm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    if uid in PENDING_USER_ACTION:
        await execute_pending_action(uid, PENDING_USER_ACTION[uid], update=update)
    else:
        await do_create(uid, update=update)


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    PENDING_CREATES.pop(uid, None)
    WIZARD_INPUT.pop(uid, None)
    if uid in PENDING_USER_ACTION:
        PENDING_ACTIONS.pop(PENDING_USER_ACTION.pop(uid), None)
    PENDING_CONFIRM_TEXT.pop(uid, None)
    await update.message.reply_text("Cancelled.")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    try:
        account_name = list_account_from_args(
            update.effective_user.id, update.message.text or ""
        )
        await update.message.reply_html(
            await list_text(update.effective_user.id, account_name)
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    try:
        parts = (update.message.text or "").split()
        if len(parts) == 2 and parts[1].isdigit():
            state = get_wizard(update.effective_user.id)
            if state.get("account_mode") != "specific":
                raise BotError(
                    "Active account random/roundrobin/spread/smart. Pakai /delete ACCOUNT LINODE_ID"
                )
            account = state.get("account") or default_account_name()
            linode_id = int(parts[1])
        elif len(parts) == 3 and parts[2].isdigit():
            account = sanitize_account_name(parts[1].split("=", 1)[-1])
            get_account(account)
            linode_id = int(parts[2])
        else:
            raise BotError("Usage: /delete LINODE_ID atau /delete ACCOUNT LINODE_ID")
        target = {"account": account, "id": linode_id, "label": "manual"}
        action_id = create_pending_action(
            update.effective_user.id, "delete", [target], {"source": "cli"}
        )
        audit_event(
            "linode.delete.requested",
            "requested",
            update=update,
            account=account,
            resource={"type": "linode", "id": linode_id},
        )
        await update.message.reply_html(
            pending_action_text(action_id),
            reply_markup=pending_action_keyboard(action_id),
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def regions_text(force: bool = False) -> str:
    regions = await regions_catalog(force=force)
    lines = ["<b>Regions</b>"]
    for r in regions[:100]:
        lines.append(f"<code>{esc(r.get('id'))}</code> - {esc(r.get('label'))}")
    return "\n".join(lines)


async def regions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    try:
        await update.message.reply_html(await regions_text())
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def types_text(force: bool = False) -> str:
    types = await types_catalog(force=force)
    lines = ["<b>Types</b>"]
    for t in types[:100]:
        price = t.get("price") or {}
        lines.append(
            f"<code>{esc(t.get('id'))}</code> - {esc(t.get('label'))} ${esc(price.get('monthly', '-'))}/mo ${esc(price.get('hourly', '-'))}/hr"
        )
    return "\n".join(lines)


async def types_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    try:
        await update.message.reply_html(await types_text())
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def images_text(force: bool = False) -> str:
    images = await images_catalog(force=force)
    lines = ["<b>Images</b>"]
    for img in images[:100]:
        lines.append(f"<code>{esc(img.get('id'))}</code> - {esc(img.get('label'))}")
    return "\n".join(lines)


async def images_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    try:
        await update.message.reply_html(await images_text())
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def refresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    msg = await update.message.reply_text("Scraping Linode catalogs...")
    try:
        reload_accounts()
        CATALOG_CACHE.clear()
        ACCOUNT_HEALTH_CACHE.clear()
        regions, types, images = await asyncio.gather(
            regions_catalog(True), types_catalog(True), images_catalog(True)
        )
        audit_event(
            "catalog.refresh",
            "success",
            update=update,
            request={
                "regions": len(regions),
                "types": len(types),
                "images": len(images),
            },
        )
        await msg.edit_text(
            f"Scrape done: accounts={len(enabled_accounts())} regions={len(regions)} types={len(types)} images={len(images)}"
        )
    except Exception as e:
        audit_event("catalog.refresh", "failed", update=update, error=e)
        await msg.edit_text(f"Error: {e}")


async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    parts = (update.message.text or "").split()
    scope = "all"
    if len(parts) > 1 and parts[1].lower() != "all":
        scope = sanitize_account_name(parts[1])
        get_account(scope)
    await update.message.reply_html(await health_text(scope, force=True))


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    parts = (update.message.text or "").split()
    scope = "all"
    fmt = "txt"
    if len(parts) > 1:
        scope = parts[1]
    if len(parts) > 2:
        fmt = parts[2]
    if scope.lower() == "new":
        rows = LAST_CREATED.get(update.effective_user.id, [])
        await send_export(update.message, rows, fmt, "new-vps", include_secret=True)
        return
    if scope.lower() != "all":
        scope = sanitize_account_name(scope)
        get_account(scope)
    rows = await collect_instances(scope)
    await send_export(update.message, rows, fmt, f"vps-{scope}", include_secret=False)


async def export_new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    parts = (update.message.text or "").split()
    fmt = parts[1] if len(parts) > 1 else "txt"
    rows = LAST_CREATED.get(update.effective_user.id, [])
    await send_export(update.message, rows, fmt, "new-vps", include_secret=True)


async def groups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    parts = (update.message.text or "").split()
    scope = parts[1] if len(parts) > 1 else "all"
    if scope != "all":
        scope = sanitize_account_name(scope)
        get_account(scope)
    await update.message.reply_html(await groups_text(scope))


async def group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    parts = (update.message.text or "").split()
    if len(parts) < 3:
        await update.message.reply_text(
            "Usage: /group create|list|delete|reboot|export NAME [all|ACCOUNT] [key=value]"
        )
        return
    sub = parts[1].lower()
    name = sanitize_group_name(parts[2])
    scope = "all"
    if sub == "create":
        args = parse_kv(update.message.text or "", start=3)
        args["group"] = name
        args.setdefault("label", name)
        plan = await finalize_plan(
            build_plan_from_options(args, update.effective_user.id)
        )
        PENDING_CREATES[update.effective_user.id] = plan
        await update.message.reply_html(
            plan_summary(plan), reply_markup=confirm_keyboard()
        )
        return
    if len(parts) > 3 and "=" not in parts[3]:
        scope = parts[3]
    if scope != "all":
        scope = sanitize_account_name(scope)
        get_account(scope)
    targets = make_targets(await collect_group_targets(name, scope))
    if sub == "list":
        rows = await collect_group_targets(name, scope)
        await update.message.reply_html(format_rows_short(rows, f"Group {name}"))
    elif sub == "export":
        fmt = "txt"
        for p in parts[3:]:
            if p in {"txt", "json", "wa", "tg"}:
                fmt = p
        await send_export(
            update.message,
            await collect_group_targets(name, scope),
            fmt,
            f"group-{name}",
            include_secret=False,
        )
    elif sub in {"delete", "reboot"}:
        action_id = create_pending_action(
            update.effective_user.id, sub, targets, {"group": name, "scope": scope}
        )
        PENDING_CONFIRM_TEXT[update.effective_user.id] = (
            action_id if action_confirm_phrase(sub, len(targets)) else ""
        )
        await update.message.reply_html(
            pending_action_text(action_id),
            reply_markup=pending_action_keyboard(action_id),
        )
    else:
        await update.message.reply_text("Subcommand invalid.")


async def text_input_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    if uid in PENDING_CONFIRM_TEXT:
        action_id = PENDING_CONFIRM_TEXT.get(uid)
        p = PENDING_ACTIONS.get(action_id or "")
        phrase = (
            action_confirm_phrase(p.get("action"), len(p.get("targets", [])))
            if p
            else None
        )
        value = (update.message.text or "").strip()
        if phrase and value == phrase:
            await execute_pending_action(uid, action_id, update=update)
        else:
            await update.message.reply_text(
                f"Confirm salah. Ketik {phrase} atau /cancel."
            )
        return
    uid = update.effective_user.id
    mode = WIZARD_INPUT.pop(uid, "")
    if not mode:
        return
    state = get_wizard(uid)
    value = (update.message.text or "").strip()
    if mode == "label":
        state["label_prefix"] = clean_label(value)
    elif mode == "tags":
        tags = [x.strip() for x in value.split(",") if x.strip()]
        state["tags"] = tags or ["telegram-bot"]
    elif mode == "group":
        state["group"] = (
            sanitize_group_name(value)
            if value.lower() not in {"clear", "none", "-"}
            else ""
        )
    elif mode == "root_pass":
        if value.lower() in {"auto", "clear", "random"}:
            state["root_pass"] = ""
        elif len(value) < 12:
            await update.message.reply_text(
                "Password minimal 12 char. Kirim ulang atau ketik auto."
            )
            WIZARD_INPUT[uid] = "root_pass"
            return
        else:
            state["root_pass"] = value
    elif mode == "dash_region":
        dash = get_dashboard(uid)
        dash["region"] = "all" if value.lower() in {"all", "semua", "-"} else value
        await render_dashboard(update, context)
        return
    elif mode == "dash_search":
        dash = get_dashboard(uid)
        dash["search"] = "" if value.lower() in {"clear", "-"} else value
        await render_dashboard(update, context)
        return
    elif mode == "resize_type":
        targets = PENDING_RESIZE_TARGETS.pop(uid, [])
        action_id = create_pending_action(uid, "resize", targets, {"type": value})
        if action_confirm_phrase("resize", len(targets)):
            PENDING_CONFIRM_TEXT[uid] = action_id
        await update.message.reply_html(
            pending_action_text(action_id),
            reply_markup=pending_action_keyboard(action_id),
        )
        return
    await update.message.reply_html(
        await wizard_text(state), reply_markup=wizard_keyboard(state)
    )


async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    data = q.data or ""
    try:
        if data == "confirm_create":
            await do_create(uid, update=update, message=q.message)
        elif data == "cancel_create":
            PENDING_CREATES.pop(uid, None)
            await q.message.reply_text("Create cancelled.")
        elif data.startswith("act:confirm:"):
            await execute_pending_action(
                uid, data.split(":", 2)[2], update=update, message=q.message
            )
        elif data.startswith("act:cancel:"):
            aid = data.split(":", 2)[2]
            PENDING_ACTIONS.pop(aid, None)
            PENDING_USER_ACTION.pop(uid, None)
            PENDING_CONFIRM_TEXT.pop(uid, None)
            await q.message.reply_text("Action cancelled.")
        elif data == "cmd:start":
            await safe_edit_or_reply(
                update, "<b>Linode bot ready.</b>", main_keyboard()
            )
        elif data == "cmd:accounts":
            await q.message.reply_html(
                await accounts_text(uid),
                reply_markup=(await picker_keyboard("account", 0))[1],
            )
        elif data == "cmd:health":
            await q.message.reply_html(await health_text("all", force=True))
        elif data == "cmd:list":
            await q.message.reply_html(
                await list_text(uid, list_account_from_args(uid, "/list"))
            )
        elif data == "cmd:groups":
            await q.message.reply_html(await groups_text("all"))
        elif data == "cmd:export":
            await send_export(
                q.message,
                await collect_instances("all"),
                "txt",
                "all-vps",
                include_secret=False,
            )
        elif data == "cmd:regions":
            await q.message.reply_html(await regions_text())
        elif data == "cmd:types":
            await q.message.reply_html(await types_text())
        elif data == "cmd:images":
            await q.message.reply_html(await images_text())
        elif data == "cmd:refresh":
            reload_accounts()
            CATALOG_CACHE.clear()
            ACCOUNT_HEALTH_CACHE.clear()
            regions, types, images = await asyncio.gather(
                regions_catalog(True), types_catalog(True), images_catalog(True)
            )
            audit_event(
                "catalog.refresh",
                "success",
                update=update,
                request={
                    "regions": len(regions),
                    "types": len(types),
                    "images": len(images),
                },
            )
            await q.message.reply_text(
                f"Scrape done: accounts={len(enabled_accounts())} regions={len(regions)} types={len(types)} images={len(images)}"
            )
        elif data == "wiz:menu":
            await render_wizard(update, context)
        elif data == "wiz:cancel":
            WIZARDS.pop(uid, None)
            WIZARD_INPUT.pop(uid, None)
            await safe_edit_or_reply(update, "Wizard cancelled.", main_keyboard())
        elif data == "wiz:refresh":
            reload_accounts()
            CATALOG_CACHE.clear()
            ACCOUNT_HEALTH_CACHE.clear()
            regions, types, images = await asyncio.gather(
                regions_catalog(True), types_catalog(True), images_catalog(True)
            )
            await q.message.reply_text(
                f"Scrape done: accounts={len(enabled_accounts())} regions={len(regions)} types={len(types)} images={len(images)}"
            )
            await render_wizard(update, context)
        elif data == "wiz:preview":
            plan = await build_plan_from_wizard(uid)
            await safe_edit_or_reply(
                update, plan_summary(plan), wizard_keyboard(get_wizard(uid))
            )
        elif data == "wiz:build":
            plan = await build_plan_from_wizard(uid)
            PENDING_CREATES[uid] = plan
            audit_event(
                "linode.create.requested", "requested", update=update, request=plan
            )
            await safe_edit_or_reply(update, plan_summary(plan), confirm_keyboard())
        elif data.startswith("wiz:pick:"):
            _, _, kind, page_s = data.split(":", 3)
            title, kb = await picker_keyboard(kind, int(page_s))
            await safe_edit_or_reply(update, title, kb)
        elif data.startswith("wiz:set:"):
            _, _, field, value = data.split(":", 3)
            state = get_wizard(uid)
            if field == "account":
                get_account(value)
                state["account"] = value
                state["account_mode"] = "specific"
                state["smart_create"] = False
            elif field == "account_mode":
                if value not in {"random", "roundrobin", "spread", "smart"}:
                    raise BotError("account mode invalid")
                state["account_mode"] = value
                state["account"] = state.get("account") or default_account_name()
                state["smart_create"] = value == "smart"
            elif field == "region":
                if value == "random":
                    state["random_region"] = True
                    state["region_mode"] = "random"
                    state["region"] = DEFAULT_REGION
                elif value == "auto":
                    state["random_region"] = True
                    state["region_mode"] = "auto"
                    state["region"] = DEFAULT_REGION
                    state["smart_create"] = True
                else:
                    state["random_region"] = False
                    state["region_mode"] = "specific"
                    state["region"] = value
            elif field == "type":
                state["type"] = value
            elif field == "image":
                state["image"] = value
            elif field == "count":
                state["count"] = max(1, min(int(value), MAX_COUNT))
            await render_wizard(update, context)
        elif data.startswith("wiz:toggle:"):
            field = data.split(":", 2)[2]
            state = get_wizard(uid)
            if field == "random":
                state["random_region"] = not bool(state.get("random_region"))
                state["region_mode"] = (
                    "random" if state["random_region"] else "specific"
                )
            elif field == "smart":
                state["smart_create"] = not bool(state.get("smart_create"))
                if state["smart_create"]:
                    state["account_mode"] = "smart"
            elif field == "backups":
                state["backups_enabled"] = not bool(state.get("backups_enabled"))
            elif field == "private":
                state["private_ip"] = not bool(state.get("private_ip"))
            elif field == "save":
                state["save_vps"] = not bool(state.get("save_vps"))
            await render_wizard(update, context)
        elif data.startswith("wiz:ask:"):
            mode = data.split(":", 2)[2]
            WIZARD_INPUT[uid] = mode
            prompts = {
                "label": "Kirim label prefix. Contoh: <code>web-prod</code>",
                "tags": "Kirim tags pisah koma. Contoh: <code>prod,bot,web</code>",
                "group": "Kirim nama group. Contoh: <code>pasukan</code> atau <code>clear</code>",
                "root_pass": "Kirim root password min 12 char, atau <code>auto</code> untuk generated.",
            }
            await safe_edit_or_reply(
                update,
                prompts.get(mode, "Kirim value."),
                InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Menu", callback_data="wiz:menu")]]
                ),
            )
        elif data == "dash:menu":
            await render_dashboard(update, context)
        elif data in {"dash:prev", "dash:next"}:
            st = get_dashboard(uid)
            st["page"] += -1 if data.endswith("prev") else 1
            await render_dashboard(update, context)
        elif data.startswith("dash:toggle:"):
            idx = int(data.split(":")[-1])
            st = get_dashboard(uid)
            item = st.get("page_items", [])[idx]
            key = row_key(item)
            selected = st.setdefault("selected", set())
            selected.remove(key) if key in selected else selected.add(key)
            await render_dashboard(update, context)
        elif data.startswith("dash:del:") or data.startswith("dash:reboot:"):
            parts = data.split(":")
            action = "delete" if parts[1] == "del" else "reboot"
            idx = int(parts[2])
            item = get_dashboard(uid).get("page_items", [])[idx]
            action_id = create_pending_action(
                uid, action, make_targets([item]), {"source": "dashboard"}
            )
            await q.message.reply_html(
                pending_action_text(action_id),
                reply_markup=pending_action_keyboard(action_id),
            )
        elif data == "dash:selectpage":
            st = get_dashboard(uid)
            selected = st.setdefault("selected", set())
            for item in st.get("page_items", []):
                selected.add(row_key(item))
            await render_dashboard(update, context)
        elif data == "dash:clear":
            get_dashboard(uid)["selected"] = set()
            await render_dashboard(update, context)
        elif data.startswith("dash:filter:"):
            st = get_dashboard(uid)
            field = data.split(":")[-1]
            if field == "account":
                choices = ["all"] + [a["name"] for a in enabled_accounts()]
                st["account"] = (
                    choices[
                        (choices.index(st.get("account", "all")) + 1) % len(choices)
                    ]
                    if st.get("account", "all") in choices
                    else "all"
                )
            elif field == "status":
                choices = [
                    "all",
                    "running",
                    "offline",
                    "booting",
                    "provisioning",
                    "rebooting",
                    "deleting",
                    "error",
                ]
                st["status"] = (
                    choices[(choices.index(st.get("status", "all")) + 1) % len(choices)]
                    if st.get("status", "all") in choices
                    else "all"
                )
            st["page"] = 0
            await render_dashboard(update, context)
        elif data == "dash:ask:region":
            WIZARD_INPUT[uid] = "dash_region"
            await q.message.reply_html(
                "Kirim region filter, contoh <code>id-cgk</code> atau <code>all</code>."
            )
        elif data == "dash:ask:search":
            WIZARD_INPUT[uid] = "dash_search"
            await q.message.reply_html(
                "Kirim search label/id/ip, atau <code>clear</code>."
            )
        elif data.startswith("dash:bulk:") or data.startswith("dash:all:"):
            st = get_dashboard(uid)
            selected = st.get("selected", set())
            all_rows = st.get("all_filtered", [])
            rows = (
                all_rows
                if data.startswith("dash:all:")
                else [r for r in all_rows if row_key(r) in selected]
            )
            if not rows:
                await q.message.reply_text("No selected targets.")
                return
            action = data.split(":")[-1]
            targets = make_targets(rows)
            if action == "export":
                await send_export(
                    q.message, rows, "txt", "selected-vps", include_secret=False
                )
                return
            if action == "resize":
                PENDING_RESIZE_TARGETS[uid] = targets
                WIZARD_INPUT[uid] = "resize_type"
                await q.message.reply_html(
                    "Kirim type baru. Contoh: <code>g6-standard-2</code>"
                )
                return
            action_id = create_pending_action(uid, action, targets, {"source": data})
            if action_confirm_phrase(action, len(targets)):
                PENDING_CONFIRM_TEXT[uid] = action_id
            await q.message.reply_html(
                pending_action_text(action_id),
                reply_markup=pending_action_keyboard(action_id),
            )
        elif data.startswith("export:new:"):
            fmt = data.split(":")[-1]
            await send_export(
                q.message,
                LAST_CREATED.get(uid, []),
                fmt,
                "new-vps",
                include_secret=True,
            )
    except Exception as e:
        await q.message.reply_text(f"Error: {e}")


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN kosong")
    if not ALLOWED_USER_IDS:
        raise SystemExit("ALLOWED_USER_IDS kosong")
    reload_accounts()
    if not enabled_accounts():
        log.warning(
            "No enabled Linode accounts. Gunakan /token add di bot untuk menambahkan."
        )
    app: Application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start"], start))
    app.add_handler(CommandHandler(["help"], help_cmd))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler(["accounts", "apis"], accounts_cmd))
    app.add_handler(CommandHandler("token", token_cmd))
    app.add_handler(CommandHandler(["account", "api"], account_cmd))
    app.add_handler(CommandHandler(["wizard", "visual"], wizard_cmd))
    app.add_handler(CommandHandler(["dashboard", "dash"], dashboard_cmd))
    app.add_handler(CommandHandler("create", create_cmd))
    app.add_handler(CommandHandler("confirm", confirm_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("regions", regions_cmd))
    app.add_handler(CommandHandler("types", types_cmd))
    app.add_handler(CommandHandler("images", images_cmd))
    app.add_handler(CommandHandler(["refresh", "scrape"], refresh_cmd))
    app.add_handler(CommandHandler(["quota", "health", "capacity"], health_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("export_new", export_new_cmd))
    app.add_handler(CommandHandler("groups", groups_cmd))
    app.add_handler(CommandHandler("group", group_cmd))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input_handler))
    log.info("Bot started accounts=%s", ",".join(a["name"] for a in enabled_accounts()))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
