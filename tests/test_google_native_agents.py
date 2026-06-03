import unittest

from wactorz.agents.calendar_agent import CalendarAgent
from wactorz.agents.gmail_agent import GmailAgent


class GoogleNativeAgentTests(unittest.TestCase):
    def test_calendar_agent_accepts_catalog_llm_provider_kwarg(self):
        agent = CalendarAgent(llm_provider=object(), persistence_dir="./state/test")
        self.assertEqual(agent.name, "calendar-agent")

    def test_gmail_agent_accepts_catalog_llm_provider_kwarg(self):
        agent = GmailAgent(llm_provider=object(), persistence_dir="./state/test")
        self.assertEqual(agent.name, "gmail-agent")


if __name__ == "__main__":
    unittest.main()
