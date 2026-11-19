"""Regression tests for the V3 domain registry."""
from __future__ import annotations

import unittest

from app.rag.domains import DomainPack, DomainRegistry, default_registry


class DomainRegistryTests(unittest.TestCase):
    def test_default_domains_are_described_and_ordered(self):
        registry = default_registry()
        self.assertEqual(registry.names(), ("policy", "manual", "retail"))
        policy = registry.get("policy")
        self.assertTrue(policy.supports_sql)
        self.assertEqual(policy.document_sources, ("policy", "bulletin"))
        manual = registry.get("manual")
        self.assertFalse(manual.supports_sql)
        self.assertTrue(registry.describe()["manual"]["supports_upload"])
        self.assertFalse(registry.describe()["manual"]["handler_bound"])

    def test_duplicate_and_unknown_domains_fail_closed(self):
        registry = DomainRegistry()
        pack = DomainPack("policy", "policy", ("policy",))
        registry.register(pack)
        with self.assertRaises(ValueError):
            registry.register(pack)
        with self.assertRaises(KeyError):
            registry.get("unknown")
        with self.assertRaises(ValueError):
            registry.register(DomainPack("", "empty", ("x",)))

    def test_handlers_are_bound_once_and_fail_closed(self):
        registry = default_registry()
        async def handler(_body, _request, _mode):
            return {"kind": "answer"}
        registry.bind("manual", handler)
        self.assertIs(registry.handler("manual"), handler)
        self.assertTrue(registry.describe()["manual"]["handler_bound"])
        with self.assertRaises(ValueError):
            registry.bind("manual", handler)
        with self.assertRaises(KeyError):
            registry.handler("policy")
