"""Command-line chat with hagent."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from hagent.config import ConfigError, Settings


def confirm_in_terminal(domain: str, service: str, data: dict[str, Any]) -> bool:
    print(f"\nhagent wants to call {domain}.{service} with {json.dumps(data)}")
    return input("Approve? [y/N] ").strip().lower() in {"y", "yes"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hagent", description=__doc__)
    parser.add_argument("prompt", nargs="*", help="One-shot prompt. Omit to chat.")
    args = parser.parse_args(argv)

    try:
        settings = Settings.from_env()
    except ConfigError as err:
        print(err, file=sys.stderr)
        return 2

    # Import here so that --help works without the SDK configured.
    import anthropic

    from hagent.agent import Agent
    from hagent.ha import HomeAssistantClient
    from hagent.tools import Toolbox

    with HomeAssistantClient(settings.ha_url, settings.ha_token) as ha:
        toolbox = Toolbox(
            ha, allow_actions=settings.allow_actions, confirm=confirm_in_terminal
        )
        agent = Agent(anthropic.Anthropic().messages, toolbox, model=settings.model)

        if args.prompt:
            print(agent.ask(" ".join(args.prompt)))
            return 0

        mode = "actions need approval" if settings.allow_actions else "read-only"
        print(f"hagent ({settings.model}, {mode}). Ctrl-D to exit.")
        while True:
            try:
                text = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if text:
                print(agent.ask(text))


if __name__ == "__main__":
    raise SystemExit(main())
