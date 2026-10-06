"""Third-party integrations: services (Home Assistant, Google, Gmail) and agent
libraries (LangChain and LangGraph, AG2).

A module that needs an optional library imports it at the top and is imported
only by code that uses it. None of this belongs in :mod:`wactorz.ext`: that
is the dashboard's extension mechanism, and every module there is imported
when the server is built.

Explicit for the same reason as ``wactorz.interfaces``: implicit namespace
packages can absorb a same-named directory from elsewhere on ``sys.path``.
"""
