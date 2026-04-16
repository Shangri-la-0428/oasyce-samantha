"""Slash-command handler — bypasses the LLM pipeline entirely.

Solves the chicken-and-egg problem: users need to configure their API
key before they can talk to Joi, but without a key Joi can't understand
"help me configure a key". Slash commands are parsed and handled before
the message ever reaches the LLM.

Security model:
  - sender_id comes from Go backend JWT auth — cannot be spoofed
  - Webhook listens on 127.0.0.1 only — no external access
  - Session workspace is ``users/{int(sender_id)}/`` — no path traversal
  - API keys are never logged or echoed in full
  - No shell execution — commands only write JSON files
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from .server import Samantha

logger = logging.getLogger(__name__)

# ── Provider registry (domain → info) ─────────────────────────


@dataclass(frozen=True)
class ProviderInfo:
    name: str
    default_model: str
    models: tuple[str, ...]


# Keyed by domain — auto-detect provider from URL
KNOWN_DOMAINS: dict[str, ProviderInfo] = {
    "api.deepseek.com": ProviderInfo(
        name="deepseek",
        default_model="deepseek-chat",
        models=("deepseek-chat", "deepseek-reasoner"),
    ),
    "api.x.ai": ProviderInfo(
        name="xai",
        default_model="grok-3-mini",
        models=("grok-3", "grok-3-mini", "grok-3-fast"),
    ),
    "api.moonshot.cn": ProviderInfo(
        name="kimi",
        default_model="moonshot-v1-auto",
        models=("moonshot-v1-auto", "moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"),
    ),
    "api.openai.com": ProviderInfo(
        name="openai",
        default_model="gpt-4o",
        models=("gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "o4-mini"),
    ),
    "api.anthropic.com": ProviderInfo(
        name="anthropic",
        default_model="claude-sonnet-4-20250514",
        models=("claude-sonnet-4-20250514", "claude-haiku-4-5-20251001"),
    ),
    "dashscope.aliyuncs.com": ProviderInfo(
        name="qwen",
        default_model="qwen-plus",
        models=("qwen-plus", "qwen-max", "qwen-turbo"),
    ),
    "generativelanguage.googleapis.com": ProviderInfo(
        name="gemini",
        default_model="gemini-2.5-flash",
        models=("gemini-2.5-flash", "gemini-2.5-pro"),
    ),
    "api.lkeap.cloud.tencent.com": ProviderInfo(
        name="tencent",
        default_model="deepseek-v3",
        models=("deepseek-v3", "deepseek-r1"),
    ),
}

# API key: printable ASCII, 8–256 chars, no whitespace
_KEY_PATTERN = re.compile(r"^[\x21-\x7E]{8,256}$")

# Accept both half-width + and full-width ＋
_PLUS_PATTERN = re.compile(r"[+＋]")


# ── Public entry point ─────────────────────────────────────────

def handle_command(
    content: str,
    samantha: "Samantha",
    sender_id: int,
) -> str | None:
    """Parse and handle a slash command. Returns response text, or
    None if the message is not a command.
    """
    text = content.strip()
    if not text.startswith("/"):
        return None

    parts = text.split()
    cmd = parts[0].lower()

    if cmd == "/key":
        return _dispatch_key(parts[1:], samantha, sender_id)
    if cmd == "/help":
        return _help_text()
    if cmd == "/start":
        return _start_tutorial(samantha, sender_id)

    return _help_text()


# ── /key ──────────────────────────────────────────────────────

def _dispatch_key(
    args: list[str],
    samantha: "Samantha",
    sender_id: int,
) -> str:
    if not args:
        return _help_key()

    # Rejoin args to handle spaces around +
    joined = " ".join(args)

    # Subcommands (no + in them)
    sub = args[0].lower()
    if sub == "show":
        return _key_show(samantha, sender_id)
    if sub == "reset":
        return _key_reset(samantha, sender_id)
    if sub == "model":
        return _key_model(args[1:], samantha, sender_id)

    # Main flow: /key <url>+<key>
    # Accept both + and ＋, with optional spaces around it
    if _PLUS_PATTERN.search(joined):
        return _key_set_url(joined, samantha, sender_id)

    return _help_key()


def _key_set_url(
    raw: str,
    samantha: "Samantha",
    sender_id: int,
) -> str:
    """``/key <url>+<key>`` — parse URL+Key, auto-detect provider."""
    # Split on first + or ＋
    parts = _PLUS_PATTERN.split(raw, maxsplit=1)
    if len(parts) != 2:
        return "格式: /key <接口地址>+<你的key>\n示例: /key https://api.deepseek.com/v1+sk-你的key"

    base_url = parts[0].strip()
    api_key = parts[1].strip()

    # ── Validate URL
    if not base_url.startswith("http"):
        return "接口地址需要以 http:// 或 https:// 开头"

    parsed = urlparse(base_url)
    domain = parsed.hostname or ""

    # ── Validate key
    if not api_key:
        return "Key 不能为空"
    if not _KEY_PATTERN.match(api_key):
        return "Key 格式不对，需要 8-256 位字符，不能有空格。"

    # ── Auto-detect provider from domain
    info = KNOWN_DOMAINS.get(domain)
    provider_name = info.name if info else "openai"  # unknown → treat as openai-compatible

    # ── Build config
    cfg = {
        "provider": provider_name,
        "api_key": api_key,
        "base_url": base_url,
    }
    if info:
        cfg["model"] = info.default_model

    # ── Write and hot-reload
    session = samantha.session(sender_id)
    llm_path = session.workspace / "llm.json"
    llm_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    try:
        from oasyce_sdk.agent.llm import load_provider
        session._user_llm = load_provider(llm_path)
    except Exception as e:
        llm_path.unlink(missing_ok=True)
        session._user_llm = None
        logger.warning("LLM config validation failed for user %d: %s", sender_id, e)
        return f"配置失败: {e}\n请检查接口地址和 Key 是否正确。"

    masked = _mask_key(api_key)
    logger.info("User %d configured LLM: provider=%s url=%s", sender_id, provider_name, domain)

    if info:
        # Known provider → show model list
        model_lines = "\n".join(
            f"  {i+1}. {m}{'  (当前)' if m == info.default_model else ''}"
            for i, m in enumerate(info.models)
        )
        return (
            f"配置成功\n"
            f"  接口: {domain}\n"
            f"  Key: {masked}\n"
            f"  模型: {info.default_model}\n"
            f"\n"
            f"可用模型:\n"
            f"{model_lines}\n"
            f"\n"
            f"切换模型请发: /key model <模型名>\n"
            f"现在可以直接发消息对话了。"
        )
    else:
        # Unknown provider → ask user to set model
        return (
            f"配置成功\n"
            f"  接口: {domain}\n"
            f"  Key: {masked}\n"
            f"\n"
            f"未识别的平台，请设置模型名:\n"
            f"  /key model <模型名>\n"
            f"\n"
            f"例如: /key model deepseek-chat"
        )


def _key_model(
    args: list[str],
    samantha: "Samantha",
    sender_id: int,
) -> str:
    """``/key model [name]`` — list or switch model."""
    session = samantha.session(sender_id)
    llm_path = session.workspace / "llm.json"

    if not llm_path.exists():
        return "还没配置，先发 /start 查看教程。"

    try:
        cfg = json.loads(llm_path.read_text(encoding="utf-8"))
    except Exception:
        return "配置文件损坏，请用 /key reset 重置。"

    base_url = cfg.get("base_url", "")
    domain = urlparse(base_url).hostname or ""
    info = KNOWN_DOMAINS.get(domain)

    # No args → list models
    if not args:
        current = cfg.get("model", "未设置")
        if info:
            model_lines = "\n".join(
                f"  {i+1}. {m}{'  (当前)' if m == current else ''}"
                for i, m in enumerate(info.models)
            )
            return f"当前模型: {current}\n\n可用模型:\n{model_lines}\n\n发送 /key model <模型名> 切换"
        return f"当前模型: {current}\n\n发送 /key model <模型名> 切换"

    new_model = args[0]

    # Support numeric selection: /key model 2
    if info and new_model.isdigit():
        idx = int(new_model) - 1
        if 0 <= idx < len(info.models):
            new_model = info.models[idx]
        else:
            return f"序号无效，请输入 1-{len(info.models)}"

    old_model = cfg.get("model")
    cfg["model"] = new_model
    llm_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    try:
        from oasyce_sdk.agent.llm import load_provider
        session._user_llm = load_provider(llm_path)
    except Exception as e:
        if old_model:
            cfg["model"] = old_model
            llm_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        session._user_llm = None
        return f"模型切换失败: {e}"

    logger.info("User %d switched model to %s", sender_id, new_model)
    return f"模型已切换为: {new_model}"


def _key_show(samantha: "Samantha", sender_id: int) -> str:
    session = samantha.session(sender_id)
    llm_path = session.workspace / "llm.json"

    if not llm_path.exists():
        default_slot = samantha._registry._default
        slot = samantha._registry._slots.get(default_slot)
        if slot:
            return f"使用平台默认配置\n  平台: {slot.provider}\n  模型: {slot.model}"
        return "使用平台默认配置"

    try:
        cfg = json.loads(llm_path.read_text(encoding="utf-8"))
    except Exception:
        return "配置文件损坏，请用 /key reset 重置。"

    masked = _mask_key(cfg.get("api_key", ""))
    domain = urlparse(cfg.get("base_url", "")).hostname or "?"
    return f"接口: {domain}\nKey: {masked}\n模型: {cfg.get('model', '未设置')}"


def _key_reset(samantha: "Samantha", sender_id: int) -> str:
    session = samantha.session(sender_id)
    llm_path = session.workspace / "llm.json"
    if llm_path.exists():
        llm_path.unlink()
    session._user_llm = None
    logger.info("User %d reset LLM config to platform default", sender_id)
    return "已恢复平台默认配置。"


# ── /start ────────────────────────────────────────────────────

def _start_tutorial(samantha: "Samantha", sender_id: int) -> str:
    session = samantha.session(sender_id)
    llm_path = session.workspace / "llm.json"

    if llm_path.exists():
        try:
            cfg = json.loads(llm_path.read_text(encoding="utf-8"))
            domain = urlparse(cfg.get("base_url", "")).hostname or "?"
            model = cfg.get("model", "未设置")
            masked = _mask_key(cfg.get("api_key", ""))
            status = f"已配置 {domain} / {model} / {masked}"
        except Exception:
            status = "配置文件损坏，请用 /key reset 重置"
    else:
        default_slot = samantha._registry._default
        slot = samantha._registry._slots.get(default_slot)
        if slot:
            status = f"平台默认 ({slot.provider} / {slot.model})"
        else:
            status = "未配置（需要设置后才能对话）"

    return (
        f"当前状态: {status}\n"
        "\n"
        "── 配置教程 ──\n"
        "\n"
        "第一步: 获取接口地址和 Key\n"
        "  去任意 AI 平台注册，找到 API 页面，\n"
        "  复制「接口地址」和「API Key」。\n"
        "\n"
        "  常见平台:\n"
        "  - DeepSeek: platform.deepseek.com\n"
        "  - xAI (Grok): console.x.ai\n"
        "  - Kimi: platform.moonshot.cn\n"
        "  - 腾讯云: console.cloud.tencent.com\n"
        "  - OpenAI: platform.openai.com\n"
        "\n"
        "第二步: 发送配置指令\n"
        "  格式: /key 接口地址+你的Key\n"
        "  中间用 + 连接，不需要空格。\n"
        "\n"
        "  示例:\n"
        "  /key https://api.deepseek.com/v1+sk-你的key\n"
        "  /key https://api.x.ai/v1+xai-你的key\n"
        "  /key https://api.moonshot.cn/v1+sk-你的key\n"
        "  /key https://api.lkeap.cloud.tencent.com/v1+sk-你的key\n"
        "\n"
        "  配置成功后会列出可用模型。\n"
        "\n"
        "第三步: 选择模型（可选）\n"
        "  配置成功后默认使用推荐模型，\n"
        "  想换可以发: /key model <模型名>\n"
        "\n"
        "── 其他指令 ──\n"
        "  /key model   查看/切换模型\n"
        "  /key show    查看当前配置\n"
        "  /key reset   恢复默认\n"
        "  /help        所有指令"
    )


# ── /help ─────────────────────────────────────────────────────

def _help_text() -> str:
    return (
        "指令列表:\n"
        "  /start                  配置教程\n"
        "  /key <地址>+<key>       配置 API\n"
        "  /key model [模型名]     查看/切换模型\n"
        "  /key show               查看当前配置\n"
        "  /key reset              恢复默认\n"
        "  /help                   显示本消息"
    )


def _help_key() -> str:
    return (
        "用法:\n"
        "  /key <接口地址>+<Key>   配置（自动识别平台和模型）\n"
        "  /key model [模型名]     查看/切换模型\n"
        "  /key show               查看当前配置\n"
        "  /key reset              恢复默认\n"
        "\n"
        "示例:\n"
        "  /key https://api.deepseek.com/v1+sk-abc123\n"
        "  /key https://api.x.ai/v1+xai-abc123\n"
        "  /key model 2"
    )


# ── Helpers ────────────────────────────────────────────────────

def _mask_key(key: str) -> str:
    if len(key) <= 10:
        return "***"
    return f"{key[:4]}***{key[-4:]}"
