"""
HomeAssistantAgent - Unified Home Assistant agent.

Handles all HA operations in a single agent:
  - recommend_hardware    : advise which devices/entities are needed
  - create_automation     : build and insert a new automation via REST
  - delete_automation     : remove an existing automation
  - edit_automation       : update an existing automation
  - list_automations      : enumerate all automations
  - list_areas            : enumerate Home Assistant areas
  - list_devices          : enumerate Home Assistant devices
  - list_entities         : enumerate Home Assistant entities


Intent is classified with a cheap single-word LLM call, then the
appropriate code path runs.  Complex operations (create, edit) use up
to two additional LLM calls internally; simpler ones (list, delete) use
one.  All HA communication goes through ha_helper.
"""

# this passes ci:
from .home_assistant_agent_ import *
#
# vs: from .home_assistant_agent__ import * (double under)
#
#
# diff:
#
# 446              # Create flow: hardware selection then automation generation.
# 447              devices = await self._get_devices()
# 448              logger.info("[%s] Got devices from Home Assistant", self.name)
# 449 -            # hardware_result = await self._select_hardware(text, devices)
# 450 -            # if not hardware_result.get("can_fulfill"):
# 451 -            #     return hardware_result
# 449 +            hardware_result = await self._select_hardware(text, devices)
# 450 +            if not hardware_result.get("can_fulfill"):
# 451 +                return hardware_result
# 452
# 453 -            # entities = self._extract_entity_ids_from_hardware(hardware_result)
# 454 -            # return await self._create_automation(text, entities, hardware_result.get("hardware", []))
# 455 -            return await self._recommend_hardware(text, devices)
# 453 +            entities = self._extract_entity_ids_from_hardware(hardware_result)
# 454 +            return await self._create_automation(text, entities, hardware_result.get("hardware", []))
# 455
# 456          return self._unsupported_action_response(text)
# 457

## this one:
#
# from .home_assistant_agent__ import *
#
## gives at ci:
##
# $> make ci
# python3 -m unittest discover -s tests -p 'test_*.py'
# [Supervisor] 'crash-once' is FAILED — applying one_for_one strategy.
# [Supervisor] 'crash-once' is FAILED — applying one_for_all strategy.
# [Supervisor] 'crash-once' is FAILED — applying rest_for_one strategy.
# ..[test-ha-agent] No LLM provider configured; skipping action classification LLM call.
# F[test-ha-agent] No LLM provider configured; skipping action classification LLM call.
# F.....
# ======================================================================
# FAIL: test_create_automation_flow_uses_existing_hardware_selection_chain (test_home_assistant_agent.HomeAssistantAgentTest.test_create_automation_flow_uses_existing_hardware_selection_chain)
# ----------------------------------------------------------------------
# Traceback (most recent call last):
#   File "/usr/lib/python3.12/unittest/async_case.py", line 90, in _callTestMethod
#     if self._callMaybeAsync(method) is not None:
#        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#   File "/usr/lib/python3.12/unittest/async_case.py", line 112, in _callMaybeAsync
#     return self._asyncioRunner.run(
#            ^^^^^^^^^^^^^^^^^^^^^^^^
#   File "/usr/lib/python3.12/asyncio/runners.py", line 118, in run
#     return self._loop.run_until_complete(task)
#            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#   File "/usr/lib/python3.12/asyncio/base_events.py", line 687, in run_until_complete
#     return future.result()
#            ^^^^^^^^^^^^^^^
#   File "/home/tam/Projects/waldiez/wactorz/tests/test_home_assistant_agent.py", line 290, in test_create_automation_flow_uses_existing_hardware_selection_chain
#     self.assertEqual(result["result"], "created")
# AssertionError: 'should not be used' != 'created'
# - should not be used
# + created


# ======================================================================
# FAIL: test_mixed_hardware_and_create_request_stays_on_create_path (test_home_assistant_agent.HomeAssistantAgentTest.test_mixed_hardware_and_create_request_stays_on_create_path)
# ----------------------------------------------------------------------
# Traceback (most recent call last):
#   File "/usr/lib/python3.12/unittest/async_case.py", line 90, in _callTestMethod
#     if self._callMaybeAsync(method) is not None:
#        ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#   File "/usr/lib/python3.12/unittest/async_case.py", line 112, in _callMaybeAsync
#     return self._asyncioRunner.run(
#            ^^^^^^^^^^^^^^^^^^^^^^^^
#   File "/usr/lib/python3.12/asyncio/runners.py", line 118, in run
#     return self._loop.run_until_complete(task)
#            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#   File "/usr/lib/python3.12/asyncio/base_events.py", line 687, in run_until_complete
#     return future.result()
#            ^^^^^^^^^^^^^^^
#   File "/home/tam/Projects/waldiez/wactorz/tests/test_home_assistant_agent.py", line 314, in test_mixed_hardware_and_create_request_stays_on_create_path
#     self.assertEqual(result, agent._select_hardware.return_value)
# AssertionError: {'result': 'hardware only'} != {'can_fulfill': False, 'result': 'missing'}
# - {'result': 'hardware only'}
# + {'can_fulfill': False, 'result': 'missing'}

# ----------------------------------------------------------------------
# Ran 9 tests in 1.089s

# FAILED (failures=2)
# make: *** [Makefile:130: test-py] Error 1
