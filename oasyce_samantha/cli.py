"""CLI commands for ``oasyce-samantha init`` and ``oasyce-samantha status``.

These are registered as subcommands on the ``oasyce-samantha`` entry
point (see ``server.main``). The no-subcommand invocation starts the
sidecar so systemd doesn't need to change.

``init`` is interactive: it walks the user through picking an App
backend profile, logging in by phone code, and configuring an LLM
provider. ``status`` prints a summary of the current config plus
liveness of the running sidecar.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

from .profiles import LOCAL, PROFILES, PUBLIC, Profile, env_override

SAMANTHA_HOME = Path.home() / ".oasyce" / "samantha"


def _choose_profile() -> Profile:
    """Interactive profile picker — env override > named profile > custom URL.

    Presented as a tiny menu with ``PUBLIC`` as the default. Everything
    visible — no magic, no hidden defaults. The environment override is
    honored silently (CI / private deployments shouldn't need to touch
    the interactive flow at all).
    """
    override = env_override()
    if override:
        print(f"Using App API from OASYCE_APP_API_BASE: {override.api_base}")
        return override

    print("App backend profile:")
    print(f"  1. public  — {PUBLIC.api_base}")
    print(f"             {PUBLIC.description}")
    print(f"  2. local   — {LOCAL.api_base}")
    print(f"             {LOCAL.description}")
    print(f"  3. custom  — type your own URL")

    choice = input("Choice [1]: ").strip() or "1"
    if choice == "1":
        return PUBLIC
    if choice == "2":
        return LOCAL
    if choice == "3":
        url = input("App API base URL: ").strip()
        if not url:
            print("No URL given.")
            sys.exit(1)
        return Profile(name="custom", api_base=url, description="custom URL")

    # Allow name-matching too: `public`, `local`
    if choice in PROFILES:
        return PROFILES[choice]

    print(f"Unknown choice: {choice}")
    sys.exit(1)


def cmd_init(args) -> None:
    """Interactive Samantha setup."""
    print("Samantha — companion setup\n")

    # 1. Pick App backend profile
    profile = _choose_profile()
    api_base = profile.api_base

    # 2. Login as the companion's account
    phone = input("\nCompanion's phone number: ").strip()
    if not phone:
        print("Phone required.")
        sys.exit(1)

    # Send verification code
    print(f"Sending code to {phone}...")
    try:
        resp = requests.post(f"{api_base}/user/phone-code", json={"phone": phone}, timeout=10)
        if resp.status_code != 200:
            print(f"Failed to send code: {resp.text}")
            sys.exit(1)
        print("Code sent.")
    except Exception as e:
        print(f"Failed: {e}")
        sys.exit(1)

    code = input("Verification code: ").strip()

    # Login
    try:
        resp = requests.post(
            f"{api_base}/user/login/phone-code",
            json={"phone": phone, "verifyCode": code},
            timeout=10,
        )
        data = resp.json()
        if resp.status_code != 200 or "data" not in data:
            print(f"Login failed: {data}")
            sys.exit(1)
    except Exception as e:
        print(f"Login failed: {e}")
        sys.exit(1)

    token = data["data"].get("token", "")
    if not token:
        print("No token in response.")
        sys.exit(1)

    # Get user info
    try:
        info_resp = requests.get(
            f"{api_base}/user/info",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        user_data = info_resp.json().get("data", {})
        user_id = user_data.get("id", 0)
        user_name = user_data.get("name", "")
    except Exception:
        user_id = 0
        user_name = ""

    print(f"Logged in as: {user_name} (ID: {user_id})")

    # 3. LLM config
    print("\nLLM provider for your companion's conversations:")
    provider = input("Provider [claude/qwen]: ").strip() or "claude"
    api_key = input("API key: ").strip()
    model = ""
    if provider == "claude":
        model = input("Model [claude-sonnet-4-20250514]: ").strip() or "claude-sonnet-4-20250514"
    elif provider == "qwen":
        model = input("Model [qwen-plus]: ").strip() or "qwen-plus"

    # 4. Write config
    SAMANTHA_HOME.mkdir(parents=True, exist_ok=True)

    config = {
        "app_api_base": api_base,
        "jwt_token": token,
        "user_id": user_id,
        "port": 8901,
        "proactive_interval": 300,
    }

    # If user provides a key, also save as platform default
    if api_key:
        config["provider"] = provider
        config["api_key"] = api_key
        config["model"] = model

    config_path = SAMANTHA_HOME / "config.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(f"\nConfig: {config_path}")

    # Also write per-user LLM config for the owner
    if api_key:
        # Need the owner's user ID — for now, prompt
        owner_id = input("\nYour user ID (the human owner): ").strip()
        if owner_id:
            user_dir = SAMANTHA_HOME / "users" / owner_id
            user_dir.mkdir(parents=True, exist_ok=True)
            llm_cfg = {"provider": provider, "api_key": api_key}
            if model:
                llm_cfg["model"] = model
            (user_dir / "llm.json").write_text(json.dumps(llm_cfg, indent=2), encoding="utf-8")
            print(f"LLM config: {user_dir / 'llm.json'}")

    print(f"""
Setup complete. Next steps:

1. Add the companion to Redis (on the server):
   redis-cli SADD samantha:agent_ids {user_id}

2. Start the sidecar:
   oasyce-samantha

3. Send a message to her in the App.
""")


def cmd_status(args) -> None:
    """Show Samantha status."""
    config_path = SAMANTHA_HOME / "config.json"
    if not config_path.exists():
        print("Samantha not configured. Run: oasyce-samantha init")
        return

    config = json.loads(config_path.read_text(encoding="utf-8"))
    print("Samantha status\n")
    print(f"  Config:     {config_path}")
    print(f"  API base:   {config.get('app_api_base', 'not set')}")
    print(f"  User ID:    {config.get('user_id', 'not set')}")
    print(f"  Port:       {config.get('port', 8901)}")
    print(f"  Platform LLM: {'configured' if config.get('api_key') else 'none'}")

    # Show user sessions
    users_dir = SAMANTHA_HOME / "users"
    if users_dir.exists():
        user_dirs = sorted(users_dir.iterdir())
        print(f"\n  Users: {len(user_dirs)}")
        for ud in user_dirs:
            has_llm = (ud / "llm.json").exists()
            has_mem = (ud / "memory.db").exists()
            print(f"    {ud.name}: LLM={'yes' if has_llm else 'no'} Memory={'yes' if has_mem else 'no'}")

    # Check if sidecar is running
    port = config.get("port", 8901)
    try:
        resp = requests.get(f"http://127.0.0.1:{port}/health", timeout=2)
        if resp.status_code == 200:
            print(f"\n  Sidecar: running on :{port}")
        else:
            print(f"\n  Sidecar: not responding")
    except Exception:
        print(f"\n  Sidecar: not running")
