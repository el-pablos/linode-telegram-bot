import json
from pathlib import Path

import pytest

import bot


@pytest.fixture(autouse=True)
def fake_accounts(monkeypatch, tmp_path):
    accounts = {
        "acc1": {"name": "acc1", "username": "user1", "token": "tok1", "enabled": True},
        "acc2": {"name": "acc2", "username": "user2", "token": "tok2", "enabled": True},
    }
    monkeypatch.setattr(bot, "ACCOUNTS", accounts)
    monkeypatch.setattr(bot, "ROUND_ROBIN_CURSOR", 0)
    monkeypatch.setattr(bot, "GEN3_VPS_FILE", str(tmp_path / "vps.json"))
    monkeypatch.setattr(bot, "AUDIT_LOG_FILE", str(tmp_path / "audit.jsonl"))
    bot.PENDING_ACTIONS.clear()
    bot.PENDING_USER_ACTION.clear()
    bot.PENDING_CONFIRM_TEXT.clear()
    bot.WIZARDS.clear()
    bot.DASHBOARDS.clear()
    bot.LAST_CREATED.clear()
    yield


def test_parse_account_modes():
    assert bot.parse_account_arg("random")[0] == "random"
    assert bot.parse_account_arg("roundrobin")[0] == "roundrobin"
    assert bot.parse_account_arg("spread")[0] == "spread"
    assert bot.parse_account_arg("all")[0] == "spread"
    assert bot.parse_account_arg("smart")[0] == "smart"
    assert bot.parse_account_arg("acc2") == ("specific", "acc2")


def test_group_tag_sanitized():
    assert bot.group_tag("My Group!!") == "group:my-group"


def test_build_plan_group_adds_group_tag():
    plan = bot.build_plan_from_options({"account": "spread", "group": "prod", "count": "2", "tags": "bot,web"})
    assert plan["account_mode"] == "spread"
    assert plan["count"] == 2
    assert "group:prod" in plan["tags"]


def test_pick_accounts_spread_distribution():
    plan = {"account_mode": "spread", "count": 5, "account": "acc1"}
    picked = [a["name"] for a in bot.pick_accounts_for_plan(plan)]
    assert picked == ["acc1", "acc2", "acc1", "acc2", "acc1"]


def test_filter_dashboard_rows_status_region_search():
    rows = [
        {"account": "acc1", "id": 1, "label": "web-prod", "region": "id-cgk", "status": "running", "ipv4": ["1.1.1.1"]},
        {"account": "acc1", "id": 2, "label": "db", "region": "sg-sin", "status": "offline", "ipv4": ["2.2.2.2"]},
    ]
    state = {"region": "id-cgk", "status": "running", "search": "web"}
    assert [r["id"] for r in bot.filter_dashboard_rows(rows, state)] == [1]
    state["search"] = "2.2.2.2"
    assert bot.filter_dashboard_rows(rows, state) == []


def test_append_to_vps_json_dedupe_and_chmod(tmp_path, monkeypatch):
    path = tmp_path / "vps.json"
    monkeypatch.setattr(bot, "GEN3_VPS_FILE", str(path))
    rows = [{"ipv4": ["1.1.1.1", "1.1.1.1", "2.2.2.2"], "password": "secret"}]
    res = bot.append_to_vps_json(rows)
    assert res["added"] == 2
    res2 = bot.append_to_vps_json(rows)
    assert res2["added"] == 0
    data = json.loads(path.read_text())
    assert len(data) == 2
    assert data[0] == {"host": "1.1.1.1", "username": "root", "password": "secret"}
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_export_redacts_existing_password_but_new_includes_when_allowed():
    rows = [{"label": "vps", "ipv4": ["1.1.1.1"], "username": "root", "password": "secret", "account": "acc1", "region": "id-cgk", "type": "g6-standard-1", "status": "running"}]
    redacted = bot.export_rows_json(rows, include_secret=False)
    assert "secret" not in redacted
    included = bot.export_rows_txt(rows, include_secret=True)
    assert "secret" in included


def test_audit_event_redacts_sensitive(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(bot, "AUDIT_LOG_FILE", str(path))
    bot.audit_event("test", request={"token": "abc", "root_pass": "secret", "safe": "ok"})
    rec = json.loads(path.read_text().splitlines()[0])
    assert rec["request"]["token"] == "***"
    assert rec["request"]["root_pass"] == "***"
    assert rec["request"]["safe"] == "ok"


@pytest.mark.asyncio
async def test_preflight_blocks_known_quota(monkeypatch):
    async def fake_health(name, force=False):
        return {"account": name, "token_ok": True, "linodes_remaining": 0, "can_create": False, "status": "block"}

    monkeypatch.setattr(bot, "account_health", fake_health)
    plan = {"account_mode": "specific", "account": "acc1", "count": 1}
    report = await bot.preflight_create(plan, force=True)
    assert report["ok"] is False
    assert "quota kurang" in report["blockers"][0]


@pytest.mark.asyncio
async def test_preflight_smart_requires_healthy_account(monkeypatch):
    async def fake_accounts_health(scope="all", force=False):
        return [
            {"account": "acc1", "token_ok": True, "linodes_remaining": 0, "can_create": False, "status": "block"},
            {"account": "acc2", "token_ok": True, "linodes_remaining": 3, "can_create": True, "status": "ok"},
        ]

    monkeypatch.setattr(bot, "accounts_health", fake_accounts_health)
    plan = {"account_mode": "smart", "count": 2}
    report = await bot.preflight_create(plan, force=True)
    assert report["ok"] is True


def test_pending_action_requires_delete_phrase_for_mass():
    targets = [{"account": "acc1", "id": 1}, {"account": "acc2", "id": 2}]
    aid = bot.create_pending_action(123, "delete", targets, {"source": "test"})
    assert bot.PENDING_USER_ACTION[123] == aid
    assert bot.action_confirm_phrase("delete", 2) == "DELETE"
    text = bot.pending_action_text(aid)
    assert "DELETE" in text


def test_classify_linode_error():
    assert bot.classify_linode_error(bot.LinodeAPIError(429, {"errors": []}, "acc1")) == "transient"
    assert bot.classify_linode_error(bot.LinodeAPIError(403, {"errors": []}, "acc1")) == "account"
    assert bot.classify_linode_error(bot.LinodeAPIError(422, {"errors": [{"reason": "region unavailable"}]}, "acc1")) == "region"
