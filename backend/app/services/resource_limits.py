"""Share process limits across independently opened backend harness contexts."""

import asyncio
from weakref import WeakKeyDictionary
from harness.config import HarnessSettings
from harness.sandbox import SandboxConfig, SandboxSlots, auto_sandbox_limit

_loops = WeakKeyDictionary()


def limits():
    loop = asyncio.get_running_loop()
    if loop not in _loops:
        settings = HarnessSettings.from_env()
        _loops[loop] = (
            SandboxSlots(
                settings.max_concurrent_sandboxes
                or auto_sandbox_limit(SandboxConfig.from_env())
            ),
            asyncio.Semaphore(settings.llm_concurrency),
        )
    return _loops[loop]
