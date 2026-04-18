"""CLI commands for ``oasyce-samantha init`` and ``oasyce-samantha status``.

These are registered as subcommands on the ``oasyce-samantha`` entry
point (see ``server.main``). The no-subcommand invocation starts the
configured runtime so systemd doesn't need to change.

``init`` is interactive: it walks the user through picking a runtime
surface (local or legacy App), then configuring the minimum needed for
that mode. ``status`` prints a summary of the current config and, for
the App adapter, the local health endpoint status.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import requests

from .profiles import LOCAL, PROFILES, PUBLIC, Profile, env_override

SAMANTHA_HOME = Path.home() / ".oasyce" / "samantha"


def _effective_adapter(config: dict) -> str:
    adapter = (config.get("adapter") or "").strip()
    if adapter:
        return adapter
    if config.get("jwt_token") or config.get("user_id") or config.get("app_api_base"):
        return "app-legacy"
    return "local"


def _choose_surface() -> str:
    print("Runtime surface:")
    print("  1. local  — standalone companion in your terminal")
    print("             no App backend required")
    print("  2. app    — connect to the legacy Oasyce App backend")
    print("             keeps webhook / websocket / social behavior")

    choice = input("Choice [1]: ").strip() or "1"
    if choice in {"1", "local"}:
        return "local"
    if choice in {"2", "app"}:
        return "app-legacy"
    print(f"Unknown choice: {choice}")
    sys.exit(1)


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


def _collect_llm_config() -> dict[str, str]:
    print("\nLLM provider for Samantha:")
    print("Leave blank if you want to configure it later with /key.")
    provider = input("Provider [claude/qwen/openai]: ").strip().lower()
    if not provider:
        return {}

    api_key = input("API key: ").strip()
    if not api_key:
        print("No API key given, skipping platform LLM setup.")
        return {}

    config = {"provider": provider, "api_key": api_key}

    if provider in {"claude", "anthropic"}:
        model = input("Model [claude-sonnet-4-20250514]: ").strip() or "claude-sonnet-4-20250514"
        config["model"] = model
    elif provider == "qwen":
        model = input("Model [qwen-plus]: ").strip() or "qwen-plus"
        config["model"] = model
    elif provider == "openai":
        model = input("Model [gpt-4o]: ").strip() or "gpt-4o"
        config["model"] = model
    else:
        model = input("Model [leave blank to set later]: ").strip()
        if model:
            config["model"] = model
        base_url = input("Custom base URL [optional]: ").strip()
        if base_url:
            config["base_url"] = base_url

    return config


def cmd_init(args) -> None:
    """Interactive Samantha setup."""
    print("Samantha — companion setup\n")

    surface = _choose_surface()
    llm_config = _collect_llm_config()

    config = {
        "adapter": surface,
        "port": 8901,
        "proactive_interval": 300,
        "local_user_id": 1,
        "local_session_id": 1,
    }
    config.update(llm_config)

    SAMANTHA_HOME.mkdir(parents=True, exist_ok=True)

    owner_id = ""
    if surface == "app-legacy":
        profile = _choose_profile()
        api_base = profile.api_base

        phone = input("\nCompanion's phone number: ").strip()
        if not phone:
            print("Phone required.")
            sys.exit(1)

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
        config.update({
            "app_api_base": api_base,
            "jwt_token": token,
            "user_id": user_id,
        })
        if llm_config:
            owner_id = input("\nYour user ID (the human owner): ").strip()

    config_path = SAMANTHA_HOME / "config.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(f"\nConfig: {config_path}")

    if llm_config and owner_id:
        user_dir = SAMANTHA_HOME / "users" / owner_id
        user_dir.mkdir(parents=True, exist_ok=True)
        (user_dir / "llm.json").write_text(json.dumps(llm_config, indent=2), encoding="utf-8")
        print(f"LLM config: {user_dir / 'llm.json'}")

    if surface == "local":
        print("""
Setup complete. Next steps:

1. Start Samantha:
   oasyce-samantha

2. Chat with her directly in your terminal.

3. If you skipped the LLM key, configure one later with:
   /key <base_url>+<api_key>
""")
        return

    print(f"""
Setup complete. Next steps:

1. Add the companion to Redis (on the server):
   redis-cli SADD samantha:agent_ids {config.get('user_id', 0)}

2. Start Samantha:
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
    adapter = _effective_adapter(config)
    print("Samantha status\n")
    print(f"  Config:     {config_path}")
    print(f"  Adapter:    {adapter}")
    print(f"  Port:       {config.get('port', 8901)}")
    print(f"  Platform LLM: {'configured' if config.get('api_key') else 'none'}")
    if adapter == "app-legacy":
        print(f"  API base:   {config.get('app_api_base', 'not set')}")
        print(f"  User ID:    {config.get('user_id', 'not set')}")
    else:
        print(f"  Local user: {config.get('local_user_id', 1)}")
        print(f"  Session ID: {config.get('local_session_id', 1)}")

    # Show user sessions
    users_dir = SAMANTHA_HOME / "users"
    if users_dir.exists():
        user_dirs = sorted(users_dir.iterdir())
        print(f"\n  Users: {len(user_dirs)}")
        for ud in user_dirs:
            has_llm = (ud / "llm.json").exists()
            has_mem = (ud / "memory.db").exists()
            print(f"    {ud.name}: LLM={'yes' if has_llm else 'no'} Memory={'yes' if has_mem else 'no'}")

    if adapter == "app-legacy":
        port = config.get("port", 8901)
        try:
            resp = requests.get(f"http://127.0.0.1:{port}/health", timeout=2)
            if resp.status_code == 200:
                print(f"\n  Sidecar: running on :{port}")
            else:
                print("\n  Sidecar: not responding")
        except Exception:
            print("\n  Sidecar: not running")
    else:
        print("\n  Sidecar: local interactive runtime (no health endpoint)")
