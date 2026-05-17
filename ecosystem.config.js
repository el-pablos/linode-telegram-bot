module.exports = {
  apps: [
    {
      name: "linode-bot",
      script: ".venv/bin/python",
      args: "bot.py",
      cwd: "/root/work/linode-telegram-bot",
      interpreter: "none",
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
