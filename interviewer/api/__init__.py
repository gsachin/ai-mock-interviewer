"""HTTP surface, extracted from ``interviewer.server`` (US-006).

Server state (``config``, ``_store``, ``_rag``) is reached by late binding
rather than import, for two reasons and only one of them is stylistic:

1. ``server`` imports this module to register the router, so a module-level
   ``from interviewer import server`` here would be circular.
2. Tests swap those attributes on the server module
   (``monkeypatch.setattr(server, "_store", ...)``). A module-level
   ``from interviewer.server import _store`` would bind the ORIGINAL object at
   import and the swap would be invisible — the seam would silently stop
   working and the tests would pass against the wrong store.
"""
