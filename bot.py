import asyncio
import html
import json
import logging
import os
import random
import re
import secrets
import string
import time
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
LINODE_TOKENS_FILE = os.getenv("LINODE_TOKENS_FILE", "tokens.json").strip() or "tokens.json"
ALLOWED_USER_IDS = {
    int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()
}
DEFAULT_REGION = os.getenv("DEFAULT_REGION", "ap-south")
DEFAULT_TYPE = os.getenv("DEFAULT_TYPE", "g6-standard-1")
DEFAULT_IMAGE = os.getenv("DEFAULT_IMAGE", "linode/ubuntu22.04")
MAX_COUNT = min(int(os.getenv("MAX_COUNT", "10")), 10)

API_BASE = "https://api.linode.com/v4"
CATALOG_TTL = 300
PAGE_SIZE = 8

ACCOUNTS: dict[str, dict[str, Any]] = {}
ROUND_ROBIN_CURSOR = 0
PENDING_CREATES: dict[int, dict[str, Any]] = {}
PENDING_DELETES: dict[int, dict[str, Any]] = {}
WIZARDS: dict[int, dict[str, Any]] = {}
WIZARD_INPUT: dict[int, str] = {}
CATALOG_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
log = logging.getLogger("linode-telegram-bot")


class BotError(Exception):
    pass


def esc(x: Any) -> str:
    return html.escape(str(x), quote=False)


def short(x: Any, n: int = 36) -> str:
    s = str(x)
    return s if len(s) <= n else s[: n - 1] + "…"


def sanitize_account_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip()).strip("-._")
    return name[:64] or "account"


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
            username = str(row.get("username") or row.get("name") or f"account-{idx}").strip()
            name = sanitize_account_name(str(row.get("name") or username or f"account-{idx}"))
            enabled = bool(row.get("enabled", True))
            accounts[name] = {
                "name": name,
                "username": username,
                "token": token,
                "enabled": enabled,
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


def reload_accounts() -> None:
    global ACCOUNTS
    ACCOUNTS = load_accounts()


def enabled_accounts() -> list[dict[str, Any]]:
    return [a for a in ACCOUNTS.values() if a.get("enabled") and a.get("token")]


def default_account_name() -> str:
    accounts = enabled_accounts()
    if not accounts:
        raise BotError("No enabled Linode accounts. Isi tokens.json atau LINODE_API_TOKEN.")
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
    return str(obj.get("account") or default_account_name())


def pick_accounts_for_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    global ROUND_ROBIN_CURSOR
    accounts = enabled_accounts()
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
    return [get_account(plan.get("account")) for _ in range(count)]


def parse_account_arg(value: str | None, fallback: dict[str, Any] | None = None) -> tuple[str, str]:
    if not value:
        fallback = fallback or {}
        return fallback.get("account_mode", "specific"), fallback.get("account") or default_account_name()
    v = value.strip()
    low = v.lower()
    if low in {"random", "rand", "acak"}:
        return "random", default_account_name()
    if low in {"roundrobin", "round-robin", "rr", "rotate"}:
        return "roundrobin", default_account_name()
    name = sanitize_account_name(v)
    get_account(name)
    return "specific", name


def is_allowed(user_id: int | None) -> bool:
    return bool(user_id and user_id in ALLOWED_USER_IDS)


async def guard(update: Update) -> bool:
    user = update.effective_user
    if not is_allowed(user.id if user else None):
        msg = "Unauthorized. Jalankan /whoami lalu masukin ID lu ke ALLOWED_USER_IDS."
        if update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(msg)
        return False
    return True


def parse_kv(text: str) -> dict[str, str]:
    parts = text.split()[1:]
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


def parse_bool(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "y", "on"}


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


async def linode_request(method: str, path: str, account: str | None = None, **kwargs: Any) -> Any:
    acct = get_account(account)
    headers = {
        "Authorization": f"Bearer {acct['token']}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(45.0)
    async with httpx.AsyncClient(base_url=API_BASE, headers=headers, timeout=timeout) as client:
        resp = await client.request(method, path, **kwargs)
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise BotError(f"Linode API {resp.status_code} [{acct['name']}]: {detail}")
    if resp.status_code == 204 or not resp.content:
        return None
    return resp.json()


async def get_paginated(path: str, params: dict[str, Any] | None = None, account: str | None = None) -> list[dict[str, Any]]:
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


async def cached_catalog(key: str, path: str, params: dict[str, Any] | None = None, force: bool = False) -> list[dict[str, Any]]:
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


def default_wizard() -> dict[str, Any]:
    return {
        "account": default_account_name(),
        "account_mode": "specific",
        "region": DEFAULT_REGION,
        "random_region": False,
        "type": DEFAULT_TYPE,
        "image": DEFAULT_IMAGE,
        "label_prefix": "linode-bot",
        "count": 1,
        "root_pass": "",
        "tags": ["telegram-bot"],
        "backups_enabled": False,
        "private_ip": False,
    }


def get_wizard(user_id: int) -> dict[str, Any]:
    if user_id not in WIZARDS:
        WIZARDS[user_id] = default_wizard()
    return WIZARDS[user_id]


async def build_plan_from_args(update: Update) -> dict[str, Any]:
    args = parse_kv(update.effective_message.text or "")
    state = get_wizard(update.effective_user.id)
    account_mode, account = parse_account_arg(args.get("account") or args.get("api"), state)
    count = int(args.get("count", "1"))
    if count < 1 or count > MAX_COUNT:
        raise BotError(f"count wajib 1-{MAX_COUNT}")

    region = args.get("region", DEFAULT_REGION)
    random_region = parse_bool(args.get("random_region", "false")) or region.lower() in {"random", "rand", "acak"}
    if random_region:
        region = "random"

    type_id = args.get("type", DEFAULT_TYPE)
    image = args.get("image", DEFAULT_IMAGE)
    label_prefix = clean_label(args.get("label", args.get("label_prefix", "linode-bot")))
    root_pass = args.get("root_pass") or gen_password()
    tags = [x.strip() for x in args.get("tags", "telegram-bot").split(",") if x.strip()]

    plan = {
        "account": account,
        "account_mode": account_mode,
        "region": region,
        "random_region": random_region,
        "type": type_id,
        "image": image,
        "label_prefix": label_prefix,
        "count": count,
        "root_pass": root_pass,
        "tags": tags,
        "backups_enabled": parse_bool(args.get("backups", "false")),
        "private_ip": parse_bool(args.get("private_ip", "false")),
    }

    info = await type_info(type_id)
    if not info:
        raise BotError(f"type tidak ketemu: {type_id}")
    plan["type_info"] = info
    return plan


async def build_plan_from_wizard(user_id: int) -> dict[str, Any]:
    state = get_wizard(user_id)
    count = int(state.get("count", 1))
    if count < 1 or count > MAX_COUNT:
        raise BotError(f"count wajib 1-{MAX_COUNT}")
    info = await type_info(state["type"])
    if not info:
        raise BotError(f"type tidak ketemu: {state['type']}")
    return {
        "account": state.get("account") or default_account_name(),
        "account_mode": state.get("account_mode", "specific"),
        "region": "random" if state.get("random_region") else state["region"],
        "random_region": bool(state.get("random_region")),
        "type": state["type"],
        "image": state["image"],
        "label_prefix": clean_label(state.get("label_prefix") or "linode-bot"),
        "count": count,
        "root_pass": state.get("root_pass") or gen_password(),
        "tags": list(state.get("tags") or ["telegram-bot"]),
        "backups_enabled": bool(state.get("backups_enabled")),
        "private_ip": bool(state.get("private_ip")),
        "type_info": info,
    }


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
        f"type: <code>{esc(plan['type'])}</code>",
        f"image: <code>{esc(plan['image'])}</code>",
        f"label: <code>{esc(plan['label_prefix'])}-01..</code>",
        f"count: <b>{plan['count']}</b>",
        f"backups: <code>{plan['backups_enabled']}</code>",
        f"private_ip: <code>{plan['private_ip']}</code>",
        f"tags: <code>{esc(','.join(plan['tags']))}</code>",
        f"est: <b>${hourly:.4f}/hour</b> | <b>${monthly:.2f}/month</b>",
    ]
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
            "Pilih via tombol. Catalog di-scrape live dari Linode API.",
            "",
            f"👤 account: <code>{esc(account_choice_text(state))}</code>",
            f"🌍 region: <code>{esc(region_txt)}</code>",
            f"📦 type: <code>{esc(state['type'])}</code> ({esc(price_txt)})",
            f"💿 image: <code>{esc(state['image'])}</code>",
            f"🔢 count: <b>{state['count']}</b>/<b>{MAX_COUNT}</b>",
            f"🏷 label: <code>{esc(state['label_prefix'])}</code>",
            f"💾 backups: <code>{state['backups_enabled']}</code>",
            f"🔒 private_ip: <code>{state['private_ip']}</code>",
            f"🏷 tags: <code>{esc(','.join(state.get('tags') or []))}</code>",
            f"🔑 root_pass: <code>{esc(root_txt)}</code>",
        ]
    )


def wizard_keyboard(state: dict[str, Any]) -> InlineKeyboardMarkup:
    rr = "ON" if state.get("random_region") else "OFF"
    backups = "ON" if state.get("backups_enabled") else "OFF"
    private = "ON" if state.get("private_ip") else "OFF"
    rows = [
        [InlineKeyboardButton("👤 Account", callback_data="wiz:pick:account:0"), InlineKeyboardButton("🌍 Region", callback_data="wiz:pick:region:0")],
        [InlineKeyboardButton(f"🎲 Random region {rr}", callback_data="wiz:toggle:random"), InlineKeyboardButton("📦 Plan/Type", callback_data="wiz:pick:type:0")],
        [InlineKeyboardButton("💿 Image", callback_data="wiz:pick:image:0"), InlineKeyboardButton("🔢 Count", callback_data="wiz:pick:count:0")],
        [InlineKeyboardButton("🏷 Label", callback_data="wiz:ask:label"), InlineKeyboardButton("🏷 Tags", callback_data="wiz:ask:tags")],
        [InlineKeyboardButton(f"💾 Backups {backups}", callback_data="wiz:toggle:backups"), InlineKeyboardButton(f"🔒 Private IP {private}", callback_data="wiz:toggle:private")],
        [InlineKeyboardButton("🔑 Root pass", callback_data="wiz:ask:root_pass"), InlineKeyboardButton("🔄 Scrape/Refresh", callback_data="wiz:refresh")],
        [InlineKeyboardButton("👀 Preview", callback_data="wiz:preview"), InlineKeyboardButton("✅ Build + Confirm", callback_data="wiz:build")],
        [InlineKeyboardButton("❌ Cancel", callback_data="wiz:cancel")],
    ]
    return InlineKeyboardMarkup(rows)


def nav_rows(kind: str, page: int, total: int) -> list[list[InlineKeyboardButton]]:
    last = max((total - 1) // PAGE_SIZE, 0)
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"wiz:pick:{kind}:{page - 1}"))
    nav.append(InlineKeyboardButton("⬅️ Menu", callback_data="wiz:menu"))
    if page < last:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"wiz:pick:{kind}:{page + 1}"))
    return [nav]


async def picker_keyboard(kind: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    page = max(page, 0)
    if kind == "account":
        items = enabled_accounts()
        start = page * PAGE_SIZE
        shown = items[start : start + PAGE_SIZE]
        rows = [
            [InlineKeyboardButton("🎲 RANDOM ACCOUNT PER VPS", callback_data="wiz:set:account_mode:random")],
            [InlineKeyboardButton("🔁 ROUND-ROBIN ACCOUNTS", callback_data="wiz:set:account_mode:roundrobin")],
        ]
        for a in shown:
            rows.append([InlineKeyboardButton(f"👤 {a['name']} ({short(a.get('username'), 28)})", callback_data=f"wiz:set:account:{a['name']}")])
        rows += nav_rows(kind, page, len(items))
        title = f"<b>👤 Pilih Account/API</b> page {page + 1}/{max((len(items) - 1) // PAGE_SIZE + 1, 1)}"
        return title, InlineKeyboardMarkup(rows)
    if kind == "region":
        items = await regions_catalog()
        start = page * PAGE_SIZE
        shown = items[start : start + PAGE_SIZE]
        rows = [
            [InlineKeyboardButton(f"{r.get('id')} - {short(r.get('label'), 26)}", callback_data=f"wiz:set:region:{r.get('id')}")]
            for r in shown
        ]
        rows.insert(0, [InlineKeyboardButton("🎲 RANDOM REGION PER VPS", callback_data="wiz:set:region:random")])
        title = f"<b>🌍 Pilih Region</b> page {page + 1}/{max((len(items) - 1) // PAGE_SIZE + 1, 1)}"
    elif kind == "type":
        items = await types_catalog()
        start = page * PAGE_SIZE
        shown = items[start : start + PAGE_SIZE]
        rows = []
        for t in shown:
            price = t.get("price") or {}
            txt = f"{t.get('id')} ${price.get('monthly', '-')}/mo"
            rows.append([InlineKeyboardButton(short(txt, 55), callback_data=f"wiz:set:type:{t.get('id')}")])
        title = f"<b>📦 Pilih Plan/Type</b> page {page + 1}/{max((len(items) - 1) // PAGE_SIZE + 1, 1)}"
    elif kind == "image":
        items = await images_catalog()
        start = page * PAGE_SIZE
        shown = items[start : start + PAGE_SIZE]
        rows = []
        for img in shown:
            iid = img.get("id")
            label = img.get("label") or iid
            rows.append([InlineKeyboardButton(short(f"{label} ({iid})", 55), callback_data=f"wiz:set:image:{iid}")])
        title = f"<b>💿 Pilih Image</b> page {page + 1}/{max((len(items) - 1) // PAGE_SIZE + 1, 1)}"
    elif kind == "count":
        rows = []
        nums = list(range(1, MAX_COUNT + 1))
        for i in range(0, len(nums), 5):
            rows.append([InlineKeyboardButton(str(n), callback_data=f"wiz:set:count:{n}") for n in nums[i : i + 5]])
        rows += nav_rows(kind, page, MAX_COUNT)
        return "<b>🔢 Pilih jumlah VPS</b>", InlineKeyboardMarkup(rows)
    else:
        raise BotError("Picker invalid")

    rows += nav_rows(kind, page, len(items))
    return title, InlineKeyboardMarkup(rows)


async def safe_edit_or_reply(update: Update, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    if update.callback_query:
        q = update.callback_query
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception:
            await q.message.reply_html(text, reply_markup=reply_markup)
    else:
        await update.effective_message.reply_html(text, reply_markup=reply_markup)


async def render_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    state = get_wizard(uid)
    await safe_edit_or_reply(update, await wizard_text(state), wizard_keyboard(state))


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Confirm create", callback_data="confirm_create"), InlineKeyboardButton("Cancel", callback_data="cancel_create")]]
    )


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚀 Visual Create", callback_data="wiz:menu")],
            [InlineKeyboardButton("👤 Accounts", callback_data="cmd:accounts"), InlineKeyboardButton("📋 List VPS", callback_data="cmd:list")],
            [InlineKeyboardButton("🔄 Scrape Catalog", callback_data="cmd:refresh")],
            [InlineKeyboardButton("🌍 Regions", callback_data="cmd:regions"), InlineKeyboardButton("📦 Types", callback_data="cmd:types")],
            [InlineKeyboardButton("💿 Images", callback_data="cmd:images")],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await update.message.reply_html(
        "<b>Linode bot ready.</b>\n"
        "\n"
        "Visual builder: /create atau /wizard\n"
        "CLI create:\n"
        "<code>/create account=random region=random type=g6-standard-1 image=linode/ubuntu22.04 label=test count=3</code>\n"
        "\n"
        "Cmd: /accounts /account /list /delete /regions /types /images /refresh /whoami",
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
        "Visual: <code>/wizard</code> atau <code>/create</code>\n\n"
        "Accounts:\n"
        "<code>/accounts</code>\n"
        "<code>/account xia-yazidjaidi821</code>\n"
        "<code>/account random</code> / <code>/account roundrobin</code>\n\n"
        "CLI:\n"
        "<code>/create account=xia-yazidjaidi821 region=ap-south type=g6-standard-1 image=linode/ubuntu22.04 label=web count=2</code>\n"
        "<code>/create account=random region=random type=g6-standard-1 image=linode/ubuntu22.04 label=rand count=5</code>\n"
        "<code>/create account=roundrobin region=random type=g6-standard-1 image=linode/ubuntu22.04 label=rr count=10</code>\n\n"
        "Optional:\n"
        "<code>root_pass=StrongPass123! backups=true private_ip=true tags=a,b random_region=true</code>\n\n"
        "Limit count max 10. Create selalu butuh confirm."
    )


async def wizard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await render_wizard(update, context)


async def accounts_text(uid: int | None = None) -> str:
    reload_accounts()
    lines = ["<b>Linode Accounts</b>"]
    if uid is not None:
        try:
            lines.append(f"active: <code>{esc(account_choice_text(get_wizard(uid)))}</code>")
        except Exception:
            pass
    if not ACCOUNTS:
        lines.append("No accounts.")
    for a in ACCOUNTS.values():
        status = "ON" if a.get("enabled") else "OFF"
        lines.append(
            f"<code>{esc(a['name'])}</code> [{status}] user=<code>{esc(a.get('username', '-'))}</code> token=<code>{esc(mask_token(a.get('token', '')))}</code>"
        )
    lines.append("\nSet: <code>/account NAME</code> | <code>/account random</code> | <code>/account roundrobin</code>")
    return "\n".join(lines)


async def accounts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await update.message.reply_html(await accounts_text(update.effective_user.id))


async def account_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_html(await accounts_text(update.effective_user.id), reply_markup=(await picker_keyboard("account", 0))[1])
        return
    try:
        mode, account = parse_account_arg(parts[1], get_wizard(update.effective_user.id))
        state = get_wizard(update.effective_user.id)
        state["account_mode"] = mode
        state["account"] = account
        await update.message.reply_html(f"Active account: <code>{esc(account_choice_text(state))}</code>")
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
        await update.message.reply_html(plan_summary(plan), reply_markup=confirm_keyboard())
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


async def do_create(user_id: int, update: Update | None = None, message=None) -> None:
    plan = PENDING_CREATES.pop(user_id, None)
    if not plan:
        target = message or (update.effective_message if update else None)
        if target:
            await target.reply_text("Tidak ada pending create. Pakai /create dulu.")
        return

    created: list[dict[str, Any]] = []
    errors: list[str] = []
    account_sequence = pick_accounts_for_plan(plan)
    for i in range(1, plan["count"] + 1):
        suffix = f"-{i:02d}" if plan["count"] > 1 else ""
        label = clean_label(f"{plan['label_prefix']}{suffix}")
        region = await random_region_id() if plan.get("random_region") else plan["region"]
        acct = account_sequence[i - 1]
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
        try:
            res = await linode_request("POST", "/linode/instances", account=acct["name"], json=payload)
            res["_chosen_region"] = region
            res["_account"] = acct["name"]
            created.append(res)
            await asyncio.sleep(0.7)
        except Exception as e:
            errors.append(f"{label}@{region}/{acct['name']}: {e}")

    lines = ["<b>Create result</b>"]
    for x in created:
        ipv4 = ", ".join(x.get("ipv4") or []) or "pending"
        region = x.get("region") or x.get("_chosen_region") or "-"
        account = x.get("_account") or "-"
        lines.append(
            f"✅ <code>{esc(x.get('label'))}</code> id=<code>{esc(x.get('id'))}</code> "
            f"account=<code>{esc(account)}</code> region=<code>{esc(region)}</code> ip=<code>{esc(ipv4)}</code>"
        )
    if created:
        lines.append(f"root_pass: <code>{esc(plan['root_pass'])}</code>")
        lines.append("Simpan sekarang. Password tidak disimpan bot.")
    for err in errors:
        lines.append(f"❌ {esc(err)}")

    target = message or (update.effective_message if update else None)
    if target:
        await target.reply_html("\n".join(lines))


async def confirm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    if uid in PENDING_DELETES:
        await do_delete(uid, update=update)
    else:
        await do_create(uid, update=update)


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    PENDING_CREATES.pop(uid, None)
    PENDING_DELETES.pop(uid, None)
    WIZARD_INPUT.pop(uid, None)
    await update.message.reply_text("Cancelled.")


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


async def list_text(uid: int | None = None, account_name: str | None = None) -> str:
    if account_name == "all":
        lines = ["<b>Linodes - all accounts</b>"]
        for acct in enabled_accounts():
            try:
                instances = await get_paginated("/linode/instances", account=acct["name"])
                lines.append(f"\n<b>{esc(acct['name'])}</b> ({len(instances)})")
                for x in instances[:50]:
                    ipv4 = ", ".join(x.get("ipv4") or []) or "-"
                    lines.append(
                        f"<code>{esc(x.get('id'))}</code> <b>{esc(x.get('label'))}</b> "
                        f"{esc(x.get('region'))}/{esc(x.get('type'))} {esc(x.get('status'))} ip=<code>{esc(ipv4)}</code>"
                    )
            except Exception as e:
                lines.append(f"❌ {esc(acct['name'])}: {esc(e)}")
        return "\n".join(lines)

    if not account_name and uid is not None:
        account_name = list_account_from_args(uid, "/list")
    account_name = account_name or default_account_name()
    instances = await get_paginated("/linode/instances", account=account_name)
    if not instances:
        return f"No Linodes on {account_name}."
    lines = [f"<b>Linodes - {esc(account_name)}</b>"]
    for x in instances[:50]:
        ipv4 = ", ".join(x.get("ipv4") or []) or "-"
        lines.append(
            f"<code>{esc(x.get('id'))}</code> <b>{esc(x.get('label'))}</b> "
            f"{esc(x.get('region'))}/{esc(x.get('type'))} {esc(x.get('status'))} ip=<code>{esc(ipv4)}</code>"
        )
    return "\n".join(lines)


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    try:
        account_name = list_account_from_args(update.effective_user.id, update.message.text or "")
        await update.message.reply_html(await list_text(update.effective_user.id, account_name))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")


def delete_args(uid: int, text: str) -> tuple[str, int]:
    parts = text.split()
    if len(parts) == 2 and parts[1].isdigit():
        state = get_wizard(uid)
        if state.get("account_mode") != "specific":
            raise BotError("Active account random/roundrobin. Pakai /delete ACCOUNT LINODE_ID")
        return state.get("account") or default_account_name(), int(parts[1])
    if len(parts) == 3 and parts[2].isdigit():
        account = parts[1]
        if account.startswith("account="):
            account = account.split("=", 1)[1]
        account = sanitize_account_name(account)
        get_account(account)
        return account, int(parts[2])
    raise BotError("Usage: /delete LINODE_ID atau /delete ACCOUNT LINODE_ID")


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    try:
        account, linode_id = delete_args(update.effective_user.id, update.message.text or "")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
        return
    PENDING_DELETES[update.effective_user.id] = {"account": account, "id": linode_id}
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Confirm delete", callback_data="confirm_delete"), InlineKeyboardButton("Cancel", callback_data="cancel_delete")]]
    )
    await update.message.reply_html(
        f"Delete Linode id=<code>{linode_id}</code> account=<code>{esc(account)}</code>? Data disk akan hilang. Tekan confirm atau /confirm.",
        reply_markup=kb,
    )


async def do_delete(user_id: int, update: Update | None = None, message=None) -> None:
    pending = PENDING_DELETES.pop(user_id, None)
    target = message or (update.effective_message if update else None)
    if not pending:
        if target:
            await target.reply_text("Tidak ada pending delete. Pakai /delete ID dulu.")
        return
    account = pending["account"]
    linode_id = pending["id"]
    try:
        await linode_request("DELETE", f"/linode/instances/{linode_id}", account=account)
        await target.reply_text(f"Deleted Linode {linode_id} on {account}.")
    except Exception as e:
        await target.reply_text(f"Error: {e}")


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
            f"<code>{esc(t.get('id'))}</code> - {esc(t.get('label'))} "
            f"${esc(price.get('monthly', '-'))}/mo ${esc(price.get('hourly', '-'))}/hr"
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
        regions, types, images = await asyncio.gather(regions_catalog(True), types_catalog(True), images_catalog(True))
        await msg.edit_text(f"Scrape done: accounts={len(enabled_accounts())} regions={len(regions)} types={len(types)} images={len(images)}")
    except Exception as e:
        await msg.edit_text(f"Error: {e}")


async def text_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
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
    elif mode == "root_pass":
        if value.lower() in {"auto", "clear", "random"}:
            state["root_pass"] = ""
        elif len(value) < 12:
            await update.message.reply_text("Password minimal 12 char. Kirim ulang atau ketik auto.")
            WIZARD_INPUT[uid] = "root_pass"
            return
        else:
            state["root_pass"] = value
    await update.message.reply_html(await wizard_text(state), reply_markup=wizard_keyboard(state))


async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    data = q.data or ""

    try:
        if data == "confirm_create":
            await do_create(uid, message=q.message)
        elif data == "cancel_create":
            PENDING_CREATES.pop(uid, None)
            await q.message.reply_text("Create cancelled.")
        elif data == "confirm_delete":
            await do_delete(uid, message=q.message)
        elif data == "cancel_delete":
            PENDING_DELETES.pop(uid, None)
            await q.message.reply_text("Delete cancelled.")
        elif data == "cmd:accounts":
            await q.message.reply_html(await accounts_text(uid), reply_markup=(await picker_keyboard("account", 0))[1])
        elif data == "cmd:list":
            await q.message.reply_html(await list_text(uid, list_account_from_args(uid, "/list")))
        elif data == "cmd:regions":
            await q.message.reply_html(await regions_text())
        elif data == "cmd:types":
            await q.message.reply_html(await types_text())
        elif data == "cmd:images":
            await q.message.reply_html(await images_text())
        elif data == "cmd:refresh":
            reload_accounts()
            CATALOG_CACHE.clear()
            regions, types, images = await asyncio.gather(regions_catalog(True), types_catalog(True), images_catalog(True))
            await q.message.reply_text(f"Scrape done: accounts={len(enabled_accounts())} regions={len(regions)} types={len(types)} images={len(images)}")
        elif data == "wiz:menu":
            await render_wizard(update, context)
        elif data == "wiz:cancel":
            WIZARDS.pop(uid, None)
            WIZARD_INPUT.pop(uid, None)
            await safe_edit_or_reply(update, "Wizard cancelled.", main_keyboard())
        elif data == "wiz:refresh":
            reload_accounts()
            CATALOG_CACHE.clear()
            regions, types, images = await asyncio.gather(regions_catalog(True), types_catalog(True), images_catalog(True))
            await q.message.reply_text(f"Scrape done: accounts={len(enabled_accounts())} regions={len(regions)} types={len(types)} images={len(images)}")
            await render_wizard(update, context)
        elif data == "wiz:preview":
            plan = await build_plan_from_wizard(uid)
            await safe_edit_or_reply(update, plan_summary(plan), wizard_keyboard(get_wizard(uid)))
        elif data == "wiz:build":
            plan = await build_plan_from_wizard(uid)
            PENDING_CREATES[uid] = plan
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
            elif field == "account_mode":
                if value not in {"random", "roundrobin"}:
                    raise BotError("account mode invalid")
                state["account_mode"] = value
                state["account"] = state.get("account") or default_account_name()
            elif field == "region":
                if value == "random":
                    state["random_region"] = True
                    state["region"] = DEFAULT_REGION
                else:
                    state["random_region"] = False
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
            elif field == "backups":
                state["backups_enabled"] = not bool(state.get("backups_enabled"))
            elif field == "private":
                state["private_ip"] = not bool(state.get("private_ip"))
            await render_wizard(update, context)
        elif data.startswith("wiz:ask:"):
            mode = data.split(":", 2)[2]
            WIZARD_INPUT[uid] = mode
            if mode == "label":
                text = "Kirim label prefix. Contoh: <code>web-prod</code>"
            elif mode == "tags":
                text = "Kirim tags pisah koma. Contoh: <code>prod,bot,web</code>"
            elif mode == "root_pass":
                text = "Kirim root password min 12 char, atau <code>auto</code> untuk generated."
            else:
                text = "Kirim value."
            await safe_edit_or_reply(update, text, InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menu", callback_data="wiz:menu")]]))
    except Exception as e:
        await q.message.reply_text(f"Error: {e}")


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN kosong")
    if not ALLOWED_USER_IDS:
        raise SystemExit("ALLOWED_USER_IDS kosong")
    reload_accounts()
    if not enabled_accounts():
        raise SystemExit("No enabled Linode accounts")

    app: Application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start"], start))
    app.add_handler(CommandHandler(["help"], help_cmd))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler(["accounts", "apis"], accounts_cmd))
    app.add_handler(CommandHandler(["account", "api"], account_cmd))
    app.add_handler(CommandHandler(["wizard", "visual"], wizard_cmd))
    app.add_handler(CommandHandler("create", create_cmd))
    app.add_handler(CommandHandler("confirm", confirm_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("regions", regions_cmd))
    app.add_handler(CommandHandler("types", types_cmd))
    app.add_handler(CommandHandler("images", images_cmd))
    app.add_handler(CommandHandler(["refresh", "scrape"], refresh_cmd))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input_handler))

    log.info("Bot started accounts=%s", ",".join(a["name"] for a in enabled_accounts()))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
