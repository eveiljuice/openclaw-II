#!/usr/bin/env python3
"""
openclaw-doctor — автономный DevOps-агент для диагностики и починки OpenClaw Gateway.

REPL loop: observe → think → act → verify → repeat
Агент использует function calling (tool use) для взаимодействия с системой.

Использование:
  python3 openclaw-doctor.py                    # автоматический режим (диагностика + починка)
  python3 openclaw-doctor.py --interactive      # интерактивный REPL
  python3 openclaw-doctor.py --watch            # watchdog + автопочинка при падении
  python3 openclaw-doctor.py --check            # только проверка, без действий
  python3 openclaw-doctor.py --problem "описание проблемы"  # конкретная проблема
"""

import json
import subprocess
import sys
import os
import time
import argparse
import signal
from datetime import datetime, timezone
from typing import Any
from openai import OpenAI

# ── Конфигурация ──────────────────────────────────────────────────────

OPENAI_API_KEY = os.environ.get(
    "OPENAI_API_KEY",
    "",
)
MODEL = os.environ.get("DOCTOR_MODEL", "gpt-5.2")
MAX_ITERATIONS = int(os.environ.get("DOCTOR_MAX_ITERATIONS", "20"))
WATCH_INTERVAL = int(os.environ.get("DOCTOR_WATCH_INTERVAL", "30"))
DOCS_URL = "https://docs.openclaw.ai"
LOG_FILE = "/tmp/openclaw/doctor-agent.log"

# Telegram
TG_BOT_TOKEN = os.environ.get(
    "TG_BOT_TOKEN", ""
)
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

# ── Цвета ─────────────────────────────────────────────────────────────

class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    DIM = "\033[2m"


def log(level: str, msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    colors = {"INFO": C.GREEN, "WARN": C.YELLOW, "ERROR": C.RED, "THINK": C.CYAN, "TOOL": C.BLUE}
    c = colors.get(level, "")
    print(f"{C.DIM}[{ts}]{C.RESET} {c}[{level}]{C.RESET} {msg}")
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}] [{level}] {msg}\n")


# ── Tools (инструменты агента) ────────────────────────────────────────

def tool_check_gateway() -> dict:
    """Проверить статус OpenClaw Gateway."""
    try:
        r = subprocess.run(
            ["openclaw", "gateway", "status"],
            capture_output=True, text=True, timeout=30
        )
        output = r.stdout + r.stderr
        is_running = "Runtime: running" in output and "RPC probe: ok" in output
        return {"status": "ok" if is_running else "down", "output": output[:3000]}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def tool_get_logs(lines: int = 50, level: str = "all") -> dict:
    """Получить последние строки лога OpenClaw Gateway."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    logfile = f"/tmp/openclaw/openclaw-{today}.log"
    try:
        with open(logfile) as f:
            all_lines = f.readlines()
        if level != "all":
            all_lines = [l for l in all_lines if f'"logLevelName":"{level.upper()}"' in l]
        result = all_lines[-lines:]
        # Упрощаем JSON логи для экономии токенов
        simplified = []
        for line in result:
            try:
                j = json.loads(line.strip())
                msg = j.get("0", "")
                lvl = j.get("_meta", {}).get("logLevelName", "")
                ts = j.get("time", "")
                simplified.append(f"[{ts}] [{lvl}] {msg}")
            except:
                simplified.append(line.strip()[:200])
        return {"lines": len(simplified), "log": "\n".join(simplified)}
    except FileNotFoundError:
        return {"error": f"Log file not found: {logfile}"}
    except Exception as e:
        return {"error": str(e)}


def tool_run_doctor() -> dict:
    """Запустить openclaw doctor — проверка конфигурации."""
    try:
        r = subprocess.run(
            ["openclaw", "doctor"],
            capture_output=True, text=True, timeout=30
        )
        return {"output": (r.stdout + r.stderr)[:3000]}
    except Exception as e:
        return {"error": str(e)}


def tool_check_resources() -> dict:
    """Проверить системные ресурсы (RAM, диск, CPU, процессы)."""
    results = {}
    try:
        r = subprocess.run(["free", "-h"], capture_output=True, text=True, timeout=5)
        results["memory"] = r.stdout
    except:
        pass
    try:
        r = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=5)
        results["disk"] = r.stdout
    except:
        pass
    try:
        with open("/proc/loadavg") as f:
            results["load"] = f.read().strip()
    except:
        pass
    try:
        r = subprocess.run(
            ["ps", "aux", "--sort=-rss"],
            capture_output=True, text=True, timeout=5
        )
        results["top_processes"] = "\n".join(r.stdout.split("\n")[:10])
    except:
        pass
    try:
        r = subprocess.run(
            ["ss", "-tlnp"],
            capture_output=True, text=True, timeout=5
        )
        results["listening_ports"] = r.stdout
    except:
        pass
    return results


def tool_check_channels() -> dict:
    """Проверить статус каналов (Telegram и т.д.)."""
    try:
        r = subprocess.run(
            ["openclaw", "channels", "status", "--probe"],
            capture_output=True, text=True, timeout=30
        )
        return {"output": (r.stdout + r.stderr)[:2000]}
    except Exception as e:
        return {"error": str(e)}


def tool_run_command(command: str, timeout: int = 30) -> dict:
    """Выполнить shell-команду (только безопасные, read-only или openclaw)."""
    # Блокируем опасные команды
    dangerous = ["rm -rf", "mkfs", "dd if=", "> /dev/", "chmod 000", "kill -9 1", "shutdown", "reboot"]
    for d in dangerous:
        if d in command:
            return {"error": f"Blocked dangerous command: {command}"}

    try:
        r = subprocess.run(
            command, shell=True,
            capture_output=True, text=True, timeout=timeout
        )
        return {
            "stdout": r.stdout[:3000],
            "stderr": r.stderr[:1000],
            "returncode": r.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {timeout}s"}
    except Exception as e:
        return {"error": str(e)}


def tool_restart_gateway() -> dict:
    """Перезапустить OpenClaw Gateway через systemctl."""
    try:
        r = subprocess.run(
            ["systemctl", "--user", "restart", "openclaw-gateway.service"],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            # Ждём пока поднимется
            for _ in range(10):
                time.sleep(3)
                check = tool_check_gateway()
                if check.get("status") == "ok":
                    return {"status": "restarted", "gateway": "ok"}
            return {"status": "restarted", "gateway": "not_responding_after_30s"}
        return {"status": "failed", "error": r.stderr}
    except Exception as e:
        return {"error": str(e)}


def tool_read_config(path: str = "") -> dict:
    """Прочитать конфигурацию OpenClaw (JSON)."""
    config_path = path or os.path.expanduser("~/.openclaw/openclaw.json")
    try:
        with open(config_path) as f:
            content = f.read()
        # Пробуем JSON5 → JSON
        return {"path": config_path, "content": content[:4000]}
    except Exception as e:
        return {"error": str(e)}


def tool_fetch_docs(page: str) -> dict:
    """Получить страницу из документации OpenClaw."""
    url = f"{DOCS_URL}/{page.lstrip('/')}"
    try:
        r = subprocess.run(
            ["curl", "-sL", "--max-time", "10", url],
            capture_output=True, text=True, timeout=15
        )
        # Грубая очистка от HTML
        text = r.stdout
        # Если markdown/text — оставляем как есть
        if text.startswith("#") or text.startswith("---"):
            return {"url": url, "content": text[:4000]}
        # HTML → извлекаем текст
        import re
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return {"url": url, "content": text[:4000]}
    except Exception as e:
        return {"error": str(e)}


def tool_systemd_journal(unit: str = "openclaw-gateway.service", lines: int = 30) -> dict:
    """Прочитать systemd journal для юнита."""
    try:
        r = subprocess.run(
            ["journalctl", "--user", "-u", unit, "-n", str(lines), "--no-pager"],
            capture_output=True, text=True, timeout=15
        )
        return {"output": (r.stdout + r.stderr)[:3000]}
    except Exception as e:
        return {"error": str(e)}


def tool_kill_zombie_processes(pattern: str) -> dict:
    """Убить зомби/зависшие процессы по паттерну."""
    if not pattern or len(pattern) < 3:
        return {"error": "Pattern too short (safety)"}
    try:
        # Сначала показываем что будем убивать
        r = subprocess.run(
            f"pgrep -f '{pattern}' | head -20",
            shell=True, capture_output=True, text=True, timeout=5
        )
        pids = r.stdout.strip().split("\n")
        pids = [p for p in pids if p.strip()]
        if not pids:
            return {"status": "no_matching_processes"}

        count = len(pids)
        subprocess.run(f"pkill -f '{pattern}'", shell=True, timeout=10)
        time.sleep(2)
        # Проверяем оставшиеся
        r2 = subprocess.run(
            f"pgrep -f '{pattern}' | wc -l",
            shell=True, capture_output=True, text=True, timeout=5
        )
        remaining = int(r2.stdout.strip() or "0")
        return {"killed": count, "remaining": remaining}
    except Exception as e:
        return {"error": str(e)}


def tool_notify_telegram(message: str) -> dict:
    """Отправить уведомление в Telegram."""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return {"error": "Telegram not configured"}
    try:
        r = subprocess.run(
            [
                "curl", "-s", "-X", "POST",
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                "-d", f"chat_id={TG_CHAT_ID}",
                "-d", f"text=🩺 Doctor Agent: {message}",
                "--max-time", "10",
            ],
            capture_output=True, text=True, timeout=15
        )
        resp = json.loads(r.stdout)
        return {"ok": resp.get("ok", False)}
    except Exception as e:
        return {"error": str(e)}


# ── Tool registry ─────────────────────────────────────────────────────

TOOLS = {
    "check_gateway": tool_check_gateway,
    "get_logs": tool_get_logs,
    "run_doctor": tool_run_doctor,
    "check_resources": tool_check_resources,
    "check_channels": tool_check_channels,
    "run_command": tool_run_command,
    "restart_gateway": tool_restart_gateway,
    "read_config": tool_read_config,
    "fetch_docs": tool_fetch_docs,
    "systemd_journal": tool_systemd_journal,
    "kill_zombie_processes": tool_kill_zombie_processes,
    "notify_telegram": tool_notify_telegram,
}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "check_gateway",
            "description": "Check OpenClaw Gateway status (running, RPC probe, port)",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_logs",
            "description": "Get last N lines from OpenClaw gateway log. Use level='ERROR' or 'WARN' to filter.",
            "parameters": {
                "type": "object",
                "properties": {
                    "lines": {"type": "integer", "default": 50, "description": "Number of lines"},
                    "level": {"type": "string", "default": "all", "enum": ["all", "ERROR", "WARN", "INFO"]},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_doctor",
            "description": "Run openclaw doctor to check configuration issues",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_resources",
            "description": "Check system resources: RAM, disk, CPU load, top processes, listening ports",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_channels",
            "description": "Check channel status (Telegram, WhatsApp, etc.) with probe",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command. Use for diagnostics (cat, grep, ls, etc.) or openclaw CLI commands. Dangerous commands are blocked.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                    "timeout": {"type": "integer", "default": 30},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restart_gateway",
            "description": "Restart OpenClaw Gateway via systemctl. Use as last resort after diagnosis.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_config",
            "description": "Read OpenClaw configuration file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "", "description": "Config path (default: ~/.openclaw/openclaw.json)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_docs",
            "description": "Fetch a page from OpenClaw docs (docs.openclaw.ai). Example: 'gateway/troubleshooting'",
            "parameters": {
                "type": "object",
                "properties": {
                    "page": {"type": "string", "description": "Doc page path, e.g. 'gateway/troubleshooting'"},
                },
                "required": ["page"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "systemd_journal",
            "description": "Read systemd journal for a unit",
            "parameters": {
                "type": "object",
                "properties": {
                    "unit": {"type": "string", "default": "openclaw-gateway.service"},
                    "lines": {"type": "integer", "default": 30},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kill_zombie_processes",
            "description": "Kill zombie/hung processes matching a pattern (e.g. 'ms-playwright.*chrome')",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Process pattern to match (min 3 chars)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notify_telegram",
            "description": "Send notification to Telegram (user Timo)",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Message text"},
                },
                "required": ["message"],
            },
        },
    },
]

# ── System prompt ─────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are OpenClaw Doctor — an autonomous DevOps agent that diagnoses and fixes OpenClaw Gateway issues.

## Your environment
- Host: Linux VPS (explicitsalmon.aeza.network)
- OpenClaw Gateway: systemd service (openclaw-gateway.service), port 18789
- Node.js process, Telegram bot (@timoclawd_bot)
- Config: ~/.openclaw/openclaw.json
- Logs: /tmp/openclaw/openclaw-*.log
- Docs: https://docs.openclaw.ai

## Your workflow (REPL loop)
1. OBSERVE: Check gateway status, logs, resources
2. DIAGNOSE: Identify the root cause
3. PLAN: Decide on fix (prefer minimal, safe actions)
4. ACT: Execute the fix
5. VERIFY: Confirm the fix worked
6. REPORT: Notify via Telegram if significant

## Rules
- ALWAYS diagnose BEFORE acting. Never blind-restart.
- Prefer read-only operations first (logs, status, resources)
- Only restart gateway after understanding the cause
- Kill processes only if clearly zombie/hung
- NEVER modify openclaw.json without explicit user approval
- NEVER run destructive commands (rm -rf, etc.)
- If unsure, notify Telegram and wait for human input
- When done (fixed or needs human), output your final diagnosis

## Common issues
- OOM: high memory → clean caches → restart
- Zombie Chrome: leftover Playwright processes → kill → restart
- Port conflict: EADDRINUSE → find and kill conflicting process
- Config error: check with `openclaw doctor`
- Channel disconnect: check with `openclaw channels status --probe`
- Disk full: clean old logs → restart

## Output format
After each tool call, briefly explain what you found and what you'll do next.
When done, give a final summary with: diagnosis, actions taken, current status.
Respond in Russian (Тимо speaks Russian).
"""

# ── REPL Loop ─────────────────────────────────────────────────────────

def execute_tool(name: str, args: dict) -> str:
    """Execute a tool and return result as string."""
    func = TOOLS.get(name)
    if not func:
        return json.dumps({"error": f"Unknown tool: {name}"})

    log("TOOL", f"{name}({json.dumps(args, ensure_ascii=False)[:100]})")

    try:
        result = func(**args)
        result_str = json.dumps(result, ensure_ascii=False, indent=2)
        # Truncate large results
        if len(result_str) > 4000:
            result_str = result_str[:4000] + "\n... (truncated)"
        return result_str
    except Exception as e:
        return json.dumps({"error": str(e)})


def run_agent_loop(
    client: OpenAI,
    initial_message: str,
    max_iterations: int = MAX_ITERATIONS,
    interactive: bool = False,
) -> str:
    """Run the agent REPL loop."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": initial_message},
    ]

    iteration = 0
    final_answer = ""

    while iteration < max_iterations:
        iteration += 1
        log("THINK", f"Итерация {iteration}/{max_iterations}")

        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOL_SCHEMAS,
                tool_choice="auto",
                temperature=0.2,
                max_completion_tokens=2000,
            )
        except Exception as e:
            log("ERROR", f"API error: {e}")
            time.sleep(5)
            continue

        choice = response.choices[0]
        message = choice.message

        # Если есть текстовый ответ — показываем
        if message.content:
            log("THINK", message.content[:500])
            final_answer = message.content

        # Если нет tool calls — агент закончил
        if not message.tool_calls:
            log("INFO", "Агент завершил работу")
            break

        # Добавляем ответ ассистента
        messages.append(message.model_dump())

        # Выполняем tool calls
        for tool_call in message.tool_calls:
            fn_name = tool_call.function.name
            try:
                fn_args = json.loads(tool_call.function.arguments)
            except:
                fn_args = {}

            result = execute_tool(fn_name, fn_args)

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })

        # Интерактивный режим — спрашиваем юзера
        if interactive and iteration % 3 == 0:
            user_input = input(f"\n{C.YELLOW}[Вопрос/команда или Enter для продолжения]: {C.RESET}").strip()
            if user_input.lower() in ("quit", "exit", "q"):
                break
            if user_input:
                messages.append({"role": "user", "content": user_input})

    if iteration >= max_iterations:
        log("WARN", f"Достигнут лимит итераций ({max_iterations})")

    return final_answer


# ── Режимы работы ─────────────────────────────────────────────────────

def mode_auto(client: OpenAI, problem: str = ""):
    """Автоматическая диагностика и починка."""
    if problem:
        prompt = f"Проблема: {problem}\n\nПроведи диагностику и почини."
    else:
        prompt = (
            "Проведи полную диагностику OpenClaw Gateway:\n"
            "1. Проверь статус gateway\n"
            "2. Проверь логи на ошибки\n"
            "3. Проверь системные ресурсы\n"
            "4. Проверь каналы\n"
            "5. Если есть проблемы — почини\n"
            "6. Отправь отчёт в Telegram"
        )

    log("INFO", "🩺 OpenClaw Doctor запущен в автоматическом режиме")
    result = run_agent_loop(client, prompt)
    print(f"\n{C.BOLD}=== Итог ==={C.RESET}")
    print(result)


def mode_interactive(client: OpenAI):
    """Интерактивный REPL."""
    log("INFO", "🩺 OpenClaw Doctor — интерактивный режим (quit для выхода)")
    print(f"\n{C.BOLD}OpenClaw Doctor — интерактивный режим{C.RESET}")
    print("Опиши проблему или задай вопрос. Агент будет использовать инструменты для диагностики.\n")

    while True:
        try:
            user_input = input(f"{C.CYAN}doctor> {C.RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nВыход.")
            break

        if user_input.lower() in ("quit", "exit", "q"):
            break
        if not user_input:
            continue

        result = run_agent_loop(client, user_input, interactive=True)
        print(f"\n{C.BOLD}Результат:{C.RESET} {result}\n")


def mode_watch(client: OpenAI):
    """Watchdog — мониторинг + автопочинка при падении."""
    log("INFO", f"🩺 OpenClaw Doctor — watchdog режим (интервал: {WATCH_INTERVAL}с)")

    consecutive_failures = 0
    max_consecutive = 3

    while True:
        check = tool_check_gateway()
        if check.get("status") == "ok":
            consecutive_failures = 0
            time.sleep(WATCH_INTERVAL)
            continue

        consecutive_failures += 1
        log("WARN", f"Gateway не отвечает (попытка {consecutive_failures}/{max_consecutive})")

        if consecutive_failures < 2:
            # Даём шанс — может просто медленный ответ
            time.sleep(5)
            continue

        if consecutive_failures > max_consecutive:
            log("ERROR", "Слишком много подряд падений — уведомляю и жду")
            tool_notify_telegram(
                f"⛔ Gateway падает {consecutive_failures} раз подряд. Нужно ручное вмешательство."
            )
            time.sleep(WATCH_INTERVAL * 5)
            consecutive_failures = 0
            continue

        # Запускаем агента для диагностики
        log("INFO", "Запускаю автономную диагностику...")
        result = run_agent_loop(
            client,
            "Gateway упал (RPC probe не отвечает). Проведи диагностику, найди причину и почини. Отправь отчёт в Telegram.",
            max_iterations=15,
        )
        log("INFO", f"Агент завершил: {result[:200]}")

        # Пауза после починки
        time.sleep(WATCH_INTERVAL * 2)
        consecutive_failures = 0


def mode_check(client: OpenAI):
    """Только проверка, без действий."""
    log("INFO", "🩺 OpenClaw Doctor — режим проверки (read-only)")
    result = run_agent_loop(
        client,
        "Проведи диагностику OpenClaw Gateway БЕЗ каких-либо изменений. "
        "Только проверь статус, логи, ресурсы, каналы. Расскажи что нашёл.",
        max_iterations=10,
    )
    print(f"\n{C.BOLD}=== Отчёт ==={C.RESET}")
    print(result)


# ── Точка входа ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="OpenClaw Doctor — автономный DevOps-агент",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python3 openclaw-doctor.py                        # авто-диагностика
  python3 openclaw-doctor.py --interactive           # REPL режим
  python3 openclaw-doctor.py --watch                 # watchdog
  python3 openclaw-doctor.py --check                 # только проверка
  python3 openclaw-doctor.py --problem "Telegram не отвечает"
        """,
    )
    parser.add_argument("--interactive", "-i", action="store_true", help="Интерактивный REPL")
    parser.add_argument("--watch", "-w", action="store_true", help="Watchdog режим")
    parser.add_argument("--check", "-c", action="store_true", help="Только проверка (read-only)")
    parser.add_argument("--problem", "-p", type=str, default="", help="Описание проблемы")
    parser.add_argument("--model", "-m", type=str, default="", help="Модель AI")
    args = parser.parse_args()

    global MODEL
    if args.model:
        MODEL = args.model

    client = OpenAI(api_key=OPENAI_API_KEY)

    # Graceful shutdown
    def signal_handler(sig, frame):
        log("INFO", "Получен сигнал остановки")
        sys.exit(0)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print(f"""
{C.BOLD}╔══════════════════════════════════════╗
║     🩺 OpenClaw Doctor Agent 🩺      ║
║     Model: {MODEL:<25s}║
╚══════════════════════════════════════╝{C.RESET}
""")

    if args.interactive:
        mode_interactive(client)
    elif args.watch:
        mode_watch(client)
    elif args.check:
        mode_check(client)
    else:
        mode_auto(client, args.problem)


if __name__ == "__main__":
    main()
