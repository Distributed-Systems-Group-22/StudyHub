
import json
import os
import time
import threading
from functools import partial
from uuid import UUID

import pika
import psycopg

from app.services.job_execution import process_job, lock_source_documents


QUEUE_NAME = "synthesis.jobs"
DOCUMENT_EVENTS_QUEUE = "synthesis.document-events"
ACTIVE_JOBS = set()


def connect_rabbitmq():
    credentials = pika.PlainCredentials(
        os.environ["RABBITMQ_USER"],
        os.environ["RABBITMQ_PASSWORD"]
    )

    return pika.BlockingConnection(
        pika.ConnectionParameters(
            host=os.getenv("RABBITMQ_HOST", "rabbitmq"),
            port=5672,
            credentials=credentials,
            heartbeat=60,
            blocked_connection_timeout=30
        )
    )


def on_message(channel, method, properties, body):
    try:
        event = json.loads(body)

        if not isinstance(event, dict):
            raise ValueError("Event must be a JSON object")

        if event.get("event_type") != "synthesis.requested":
            raise ValueError("Unsupported event type")

        job_id = UUID(event["job_id"])

    except (ValueError, KeyError, TypeError) as error:
        print(f"Invalid message: {error}", flush=True)

        channel.basic_nack(
            delivery_tag=method.delivery_tag,
            requeue=False
        )
        return

    cancelled = threading.Event()
    ACTIVE_JOBS.add(cancelled)

    def finish(error):
        ACTIVE_JOBS.discard(cancelled)
        if not channel.is_open:
            cancelled.set()
            return
        if error is None:
            channel.basic_ack(delivery_tag=method.delivery_tag)
        else:
            print(f"Worker infrastructure error: {error}", flush=True)
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)

    def work():
        failure = None
        try:
            process_job(job_id, cancelled)
        except Exception as error:
            failure = error
        try:
            # Pika I/O and ACKs remain on the connection thread during inference.
            channel.connection.add_callback_threadsafe(partial(finish, failure))
        except pika.exceptions.ConnectionWrongStateError:
            cancelled.set()

    threading.Thread(target=work, daemon=True).start()


def on_document_event(channel, method, properties, body):
    try:
        event = json.loads(body)

        if not isinstance(event, dict):
            raise ValueError("Event must be a JSON object")

        event_type = (
            event.get("eventType")
            or event.get("event_type")
        )

        if event_type != "document.deleted":
            print(
                f"[Consumer] Ignoring event: {event_type}",
                flush=True
            )
            channel.basic_ack(
                delivery_tag=method.delivery_tag
            )
            return

        raw_doc_id = (
            event.get("documentId")
            or event.get("document_id")
        )

        # Validate the document UUID before accessing the DB.
        doc_id = str(UUID(str(raw_doc_id)))

    except (ValueError, TypeError, KeyError) as error:
        print(
            f"[Consumer] Invalid document event: {error}",
            flush=True
        )
        channel.basic_nack(
            delivery_tag=method.delivery_tag,
            requeue=False
        )
        return

    try:
        from app.database import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                lock_source_documents(cur, [doc_id])
                cur.execute("""
                    INSERT INTO synthesis_deleted_documents(document_id)
                    VALUES (%s) ON CONFLICT DO NOTHING
                """, (doc_id,))
                cur.execute("""
                    UPDATE synthesis_jobs
                    SET status = 'FAILED', claim_token = NULL, lease_until = NULL,
                        error_message = 'permission_revoked: source deleted', updated_at = NOW()
                    WHERE status IN ('PENDING', 'RUNNING') AND document_ids ? %s
                """, (doc_id,))
                cur.execute("""
                    UPDATE synthesis_results
                    SET status = 'INVALIDATED'
                    WHERE status = 'VALID'
                      AND summary_id IN (
                          SELECT summary_id
                          FROM summary_sources
                          WHERE document_id = %s
                      )
                """, (doc_id,))

                affected = cur.rowcount

        print(
            f"[Consumer] document.deleted: {doc_id}; "
            f"invalidated {affected} summaries",
            flush=True
        )

        # ACK only after the DB transaction commits.
        channel.basic_ack(
            delivery_tag=method.delivery_tag
        )

    except psycopg.Error as error:
        print(
            f"[Consumer] Database error while invalidating "
            f"{doc_id}: {error}",
            flush=True
        )

        # Temporary DB failure: retry the message.
        time.sleep(2)

        channel.basic_nack(
            delivery_tag=method.delivery_tag,
            requeue=True
        )

    except Exception as error:
        print(
            f"[Consumer] Unexpected deletion event error: "
            f"{error}",
            flush=True
        )

        # Do not acknowledge a message that was not processed.
        # Closing the channel lets RabbitMQ redeliver
        # the unacknowledged message after reconnection.
        raise


def main():
    while True:
        connection = None

        try:
            connection = connect_rabbitmq()
            channel = connection.channel()

            channel.queue_declare(
                queue=QUEUE_NAME,
                durable=True
            )
            channel.queue_declare(
                queue=DOCUMENT_EVENTS_QUEUE,
                durable=True
            )
            
            # Content Service publishes deletion events.
            channel.exchange_declare(
                exchange="document.events",
                exchange_type="topic",
                durable=True
            )

            channel.queue_bind(
                exchange="document.events",
                queue=DOCUMENT_EVENTS_QUEUE,
                routing_key="document.deleted"
            )

            print(
                "Document deletion events binding configured",
                flush=True
            )


            channel.basic_qos(prefetch_count=1)

            channel.basic_consume(
                queue=QUEUE_NAME,
                on_message_callback=on_message,
                auto_ack=False
            )
            channel.basic_consume(
                queue=DOCUMENT_EVENTS_QUEUE,
                on_message_callback=on_document_event,
                auto_ack=False
            )

            print(
                f"Worker consuming queues: {QUEUE_NAME}, {DOCUMENT_EVENTS_QUEUE}",
                flush=True
            )

            channel.start_consuming()

        except (pika.exceptions.AMQPError, OSError) as error:
            print(
                f"Worker connection error: {error}",
                flush=True
            )
            time.sleep(5)

        finally:
            for cancelled in ACTIVE_JOBS:
                cancelled.set()
            ACTIVE_JOBS.clear()
            if connection is not None and connection.is_open:
                connection.close()


if __name__ == "__main__":
    main()
