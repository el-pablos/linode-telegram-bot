# Linode Telegram Bot

Visual Telegram bot untuk create/list/delete Linode via Linode API.

## Setup

```bash
cd /root/work/linode-telegram-bot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp tokens.example.json tokens.json
# edit .env + tokens.json
python bot.py
```

## Multi API accounts

Secret config: `tokens.json` (gitignored).

```json
[
  {
    "name": "account-1",
    "username": "your-linode-username",
    "token": "linode-token-1",
    "enabled": true
  }
]
```

Modes:

- specific account: `account=account-1`
- random account per VPS: `account=random`
- round-robin: `account=roundrobin`

Commands:

```txt
/accounts
/account account-1
/account random
/account roundrobin
/list
/list all
/list account-1
/delete account-1 LINODE_ID
```

## Visual flow

```txt
/start
/create
/wizard
```

Pilih via button:

- Account/API token
- Random/round-robin account
- Region dari live Linode API
- Random region per VPS
- Plan/type dari live Linode API
- Image dari live Linode API
- Count 1-10
- Label, tags, root password
- Backups/private IP toggle
- Preview + confirm

## CLI flow

```txt
/create account=xia-yazidjaidi821 region=ap-south type=g6-standard-1 image=linode/ubuntu22.04 label=test count=1
/create account=random region=random type=g6-standard-1 image=linode/ubuntu22.04 label=rand count=5
/create account=roundrobin region=random type=g6-standard-1 image=linode/ubuntu22.04 label=rr count=10
```

## Commands

```txt
/start
/help
/whoami
/accounts
/account NAME
/create
/wizard
/confirm
/cancel
/list [all|ACCOUNT]
/delete [ACCOUNT] LINODE_ID
/regions
/types
/images
/refresh
/scrape
```

## Security

`.env` + `tokens.json` berisi token. Jangan commit/share. Rotate token kalau pernah bocor.
