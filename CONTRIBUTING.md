# Contributing to Threads Bot

Thanks for your interest in contributing!

> **Note:** This project is feature-complete and not actively maintained. Issues and pull requests are welcome, but responses may take a while.

## Development Setup

The project targets Linux (the worker lock uses `fcntl`).

```bash
git clone https://github.com/mkot85549-cell/Threads-Bot.git
cd threads-bot

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env    # fill in your own credentials
python migrations/migrate.py
```

See the [Quick Start](README.md#-quick-start) in the README for running the web app and the Telegram bot.

## Reporting Bugs

Open an issue and include:

- What you expected and what happened instead
- Steps to reproduce
- Python version and OS
- Relevant logs

**Remove all tokens, API keys, and personal data from logs and screenshots before posting.**

## Suggesting Changes

1. Fork the repository and create a branch: `git checkout -b feature/my-change`
2. Keep the change focused: one pull request per fix or feature.
3. Write clear commit messages that say what changed and why.
4. Make sure the code compiles: `python -m py_compile *.py`
5. Open a pull request and describe what you changed and how you tested it.

If you are planning a larger change, open an issue first to discuss it.

## Code Guidelines

- Follow the existing structure and naming conventions.
- Add type hints to new functions where practical.
- Any change to the database schema must go through `schema.py` and `migrations/migrate.py`, and migrations must stay idempotent.
- Keep Meta and Claude API calls inside `social_service.py` and `ai_service.py`.
- Do not add new dependencies without a good reason, and pin them in `requirements.txt`.

## What Not to Commit

- `.env` files, tokens, API keys, or any other secrets
- Database files (`*.db`) and `workers.lock`
- Hardcoded absolute paths or environment-specific settings

If a secret is committed by accident, revoke it immediately. Deleting it in a later commit is not enough, because it stays in the git history.

## Security Issues

Please do not report vulnerabilities in public issues. Use GitHub's private vulnerability reporting (Security tab → *Report a vulnerability*) if it is enabled for this repository.

## Responsible Use

Contributions must not make it easier to spam, impersonate, or harass. Features that automate platform interactions should respect the platform's rate limits and policies (see *Responsible Use & Platform Policy* in the README).

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
