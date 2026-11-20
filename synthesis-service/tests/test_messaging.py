import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pika
import psycopg

from app import database
from app.messaging import consumer, outbox, recovery
from tests.support import JOB_ID, SynthesisTestCase, statements


class MessagingTests(SynthesisTestCase):
    def test_empty_outbox_does_not_publish(self):
        self.database(outbox, fetches=[None])
        channel = Mock()
        self.assertFalse(outbox.publish_one(channel))
        channel.basic_publish.assert_not_called()

    def event(self):
        return {"event_id": JOB_ID, "event_type": "synthesis.requested", "payload": {"job_id": str(JOB_ID)}}

    def test_confirmed_publish_marks_event_sent_after_publish(self):
        _, cursor = self.database(outbox, fetches=[self.event()])
        channel = Mock()
        channel.basic_publish.side_effect = lambda **kw: self.assertEqual(len(statements(cursor)), 1)
        self.assertTrue(outbox.publish_one(channel))
        self.assertIn("published_at = NOW()", statements(cursor)[-1])
        self.assertTrue(channel.basic_publish.call_args.kwargs["mandatory"])

    def test_broker_failure_rolls_back_without_marking_sent(self):
        connection, cursor = self.database(outbox, fetches=[self.event()])
        channel = Mock()
        channel.basic_publish.side_effect = pika.exceptions.AMQPConnectionError("offline")
        with self.assertRaises(pika.exceptions.AMQPConnectionError):
            outbox.publish_one(channel)
        self.assertEqual(len(statements(cursor)), 1)
        self.assertIs(connection.__exit__.call_args.args[0], pika.exceptions.AMQPConnectionError)

    def test_expired_lease_schedules_recovery_in_same_transaction(self):
        connection, cursor = self.database(recovery, rows=[{"job_id": JOB_ID, "attempts": 1}])
        self.assertEqual(recovery.recover_expired_jobs(), 1)
        self.assertIn("status = 'PENDING'", statements(cursor)[1])
        self.assertIn("INSERT INTO outbox_events", statements(cursor)[2])
        connection.__exit__.assert_called_once_with(None, None, None)

    def test_expired_exhausted_job_fails_without_requeue(self):
        _, cursor = self.database(recovery, rows=[{"job_id": JOB_ID, "attempts": 3}])
        self.assertEqual(recovery.recover_expired_jobs(), 0)
        self.assertIn("status = 'FAILED'", statements(cursor)[-1])
        self.assertFalse(any("INSERT INTO" in s for s in statements(cursor)))

    def test_invalid_job_event_is_rejected_without_requeue(self):
        for body in (b"not json", b"[]", b'{"event_type":"unknown"}', b'{"event_type":"synthesis.requested","job_id":"invalid"}'):
            with self.subTest(body=body):
                channel = Mock()
                consumer.on_message(channel, SimpleNamespace(delivery_tag=1), None, body)
                channel.basic_nack.assert_called_once_with(delivery_tag=1, requeue=False)
                channel.basic_ack.assert_not_called()

    def test_successful_delivery_acknowledges_on_connection_thread(self):
        channel, method = Mock(), SimpleNamespace(delivery_tag=1)
        channel.connection.add_callback_threadsafe.side_effect = lambda callback: callback()
        with patch.object(consumer.threading, "Thread") as thread, patch.object(consumer, "process_job") as process:
            thread.side_effect = lambda **kw: SimpleNamespace(start=kw["target"])
            consumer.on_message(channel, method, None, json.dumps({"event_type": "synthesis.requested", "job_id": str(JOB_ID)}))
        process.assert_called_once()
        channel.basic_ack.assert_called_once_with(delivery_tag=1)
        self.assertFalse(consumer.ACTIVE_JOBS)

    def test_infrastructure_failure_requeues_delivery(self):
        channel = Mock()
        channel.connection.add_callback_threadsafe.side_effect = lambda callback: callback()
        with patch.object(consumer.threading, "Thread") as thread, patch.object(consumer, "process_job", side_effect=RuntimeError("offline")):
            thread.side_effect = lambda **kw: SimpleNamespace(start=kw["target"])
            consumer.on_message(channel, SimpleNamespace(delivery_tag=1), None, json.dumps({"event_type": "synthesis.requested", "job_id": str(JOB_ID)}))
        channel.basic_nack.assert_called_once_with(delivery_tag=1, requeue=True)

    def test_deletion_is_acknowledged_only_after_database_commit(self):
        connection, cursor = self.database(database)
        order = []
        connection.__exit__.side_effect = lambda *args: order.append("commit") or False
        channel = Mock()
        channel.basic_ack.side_effect = lambda **kw: order.append("ack")
        consumer.on_document_event(channel, SimpleNamespace(delivery_tag=1), None,
                                   json.dumps({"eventType": "document.deleted", "documentId": str(JOB_ID)}))
        self.assertEqual(order, ["commit", "ack"])
        self.assertTrue(any("synthesis_deleted_documents" in s for s in statements(cursor)))
        self.assertTrue(any("INVALIDATED" in s for s in statements(cursor)))

    def test_deletion_database_error_is_requeued(self):
        _, cursor = self.database(database)
        cursor.execute.side_effect = psycopg.OperationalError("offline")
        channel = Mock()
        with patch.object(consumer.time, "sleep"):
            consumer.on_document_event(channel, SimpleNamespace(delivery_tag=1), None,
                                       json.dumps({"eventType": "document.deleted", "documentId": str(JOB_ID)}))
        channel.basic_nack.assert_called_once_with(delivery_tag=1, requeue=True)
        channel.basic_ack.assert_not_called()
