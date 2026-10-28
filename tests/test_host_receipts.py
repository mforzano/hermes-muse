"""Compatibility with hosts that retired cron.bot_chat_delivery; no sending."""
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from hermes_muse.host import HermesHost

class RetiredBotChatDelivery(unittest.TestCase):
    def test_missing_legacy_receipt_reader_preserves_unknown_without_send(self):
        jobs=types.ModuleType('cron.jobs')
        jobs.get_job=lambda job_id: None
        live=types.ModuleType('tools.bot_live_delivery')
        live.read_delivery_result=lambda home,key: None
        constants=types.ModuleType('hermes_constants')
        constants.set_hermes_home_override=lambda home: None
        constants.reset_hermes_home_override=lambda token: None
        modules={'cron.bot_chat_delivery':None,'cron.jobs':jobs,'tools.bot_live_delivery':live,'hermes_constants':constants}
        host=HermesHost(types.SimpleNamespace(home=Path.cwd()))
        with patch.dict(sys.modules,modules):
            self.assertEqual(host.bot_delivery_status('missing-job'),'unknown')

if __name__=='__main__': unittest.main()
