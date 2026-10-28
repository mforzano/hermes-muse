from unittest.mock import Mock
from test_operations import OperationFixture
from hermes_muse.audit import Audit


class AuditTests(OperationFixture):
    def test_selected_cases_finish_independently_without_workers_or_raw_prefix(self):
        for n in range(3):
            self.message(role='user',text='Accepted work '+str(n));self.call(str(n))
        audit=Audit(self.ops);page=audit.batch()
        self.assertEqual(page['assignments'],[])
        self.assertEqual(len(page['cases']),3)
        result=audit.commit({'batch':page['batch'],'reviews':[{'slot':3,'summary':'Verified the current case'}]})
        self.assertEqual(result['remaining_slots'],2)
        self.assertTrue(self.store.read('operation_snapshot',page['cases'][2]['receipt'])['reviewed'])
        self.assertIsNone(self.store.read('meta','operation_cursor'))
        resumed=audit.batch();self.assertEqual(len(resumed['cases']),2)

    def test_unknown_old_owner_is_retained_never_dispatched_again(self):
        legacy={'number':339,'pages':[{'receipt':'old-receipt','reviewed':False}]}
        self.store.write('meta','operation_wave',legacy)
        self.store.write('meta','operation_cursor',{'message_id':0,'since':self.now})
        self.store.write('operation_dispatch','old-receipt',{'state':'unknown','owner_id':'native'})
        with self.store.transaction() as db:self.store.delete(db,'meta','operation_policy')
        self.ops.policy();callback=Mock()
        with self.assertRaises(ValueError):Audit(self.ops).dispatch({'goal':'Review audit batch 339 receipt old-receipt'},callback)
        callback.assert_not_called()
        self.assertEqual(self.store.read('operation_dispatch','old-receipt')['state'],'unknown')

    def test_missing_original_cannot_be_committed_as_reviewed(self):
        mid=self.call();audit=Audit(self.ops);page=audit.batch()
        self.db.execute('DELETE FROM messages WHERE id=?',(mid,));self.db.commit()
        with self.assertRaises(ValueError):audit.commit({'batch':page['batch'],'reviews':[{'slot':1,'summary':'No original'}]})
        self.assertFalse(self.store.read('operation_snapshot',page['cases'][0]['receipt'])['reviewed'])
