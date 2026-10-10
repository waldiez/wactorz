"""Everything a scenario is allowed to call.

A scenario file imports from here and nowhere else: no Playwright, no asyncio, no
broker client. That is not tidiness — it is what makes "no timing in scenarios"
hold by construction. A scenario that cannot reach `time.sleep` cannot sleep, and
a scenario that cannot reach the Playwright page cannot grow its own waits.
"""
