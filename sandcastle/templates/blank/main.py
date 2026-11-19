# Blank template: customize this to build your own orchestration.
# Run this with: python .sandcastle/main.py

import asyncio

from sandcastle import claude_code, run
from sandcastle.sandboxes.docker import docker


async def main() -> None:
    await run(
        agent=claude_code("claude-opus-4-8"),
        sandbox=docker(),
        prompt_file="./.sandcastle/prompt.md",
    )


asyncio.run(main())
