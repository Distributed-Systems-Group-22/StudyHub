"""At-least-once consumer. Keep broker heartbeats alive during slow model calls."""
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID
from datetime import datetime, timezone
from src.retry import is_transient, retry_delay
import pika
from src.clients.content_client import DocumentUnavailable

logger = logging.getLogger(__name__)


def parse_event(body):
    """Validate identifiers and the exact range supported by PostgreSQL INTEGER."""
    event = json.loads(body)
    if not isinstance(event, dict) or event.get('eventType') != 'document.uploaded':
        raise ValueError('Expected document.uploaded')
    for field in ('eventId', 'documentId'):
        if not isinstance(event.get(field), str):
            raise ValueError(f'Invalid {field}')
        event[field] = str(UUID(event[field]))
    version = event.get('documentVersion')
    if type(version) is not int or not 1 <= version <= 2147483647:
        raise ValueError('Invalid documentVersion')
    return event


def publish_result(settings, row):
    """Proposed Search handoff; receiver must deduplicate using eventId."""
    parameters = pika.URLParameters(settings.amqp_url)
    parameters.blocked_connection_timeout = 15
    with pika.BlockingConnection(parameters) as connection:
        channel = connection.channel()
        channel.exchange_declare(exchange='processing.events', exchange_type='topic', durable=True)
        channel.queue_declare(queue='search.processing-results', durable=True)
        channel.queue_bind(queue='search.processing-results', exchange='processing.events',
                           routing_key='document.processed')
        channel.confirm_delivery()
        payload = {'eventId': str(row['result_event_id']), 'eventType': 'document.processed',
                   'schemaVersion': 1, **row['result']}
        channel.basic_publish(exchange='processing.events', routing_key='document.processed',
            body=json.dumps(payload, allow_nan=False).encode(), mandatory=True,
            properties=pika.BasicProperties(content_type='application/json', delivery_mode=2,
                                             message_id=payload['eventId']))


class Handler:
    """Own durable job transitions independently of RabbitMQ connection lifetime."""
    def __init__(self, settings, store, pipeline, publisher=publish_result):
        self.settings, self.store, self.pipeline, self.publisher = settings, store, pipeline, publisher

    def handle(self, event):
        """Resume persisted work and acknowledge only terminal, delivered outcomes."""
        with self.store.lock(event) as job:
            if job is None:
                return False
            row = job.read()
            if row['state'] in ('completed', 'skipped'):
                return True
            if row.get('next_attempt_at') and row['next_attempt_at'] > datetime.now(timezone.utc):
                # The broker may redeliver immediately; the DB cooldown survives restarts.
                return False
            if row['state'] == 'pending':
                # Count before external work. A crash also consumes an attempt.
                if row['attempts'] >= self.settings.max_attempts:
                    job.save('failed', error_type='AttemptsExhausted')
                else:
                    job.attempt()
                    try:
                        result = self.pipeline.process(event['documentId'], event['documentVersion'])
                    except DocumentUnavailable:
                        job.save('skipped')
                        return True
                    except Exception as error:
                        # Do not persist exception bodies: providers can echo sensitive input.
                        logger.warning('Processing attempt failed: %s', type(error).__name__)
                        if is_transient(error):
                            delay = retry_delay(row.get('retry_count', 0), error,
                                getattr(self.settings, 'retry_base_seconds', 10),
                                getattr(self.settings, 'retry_max_seconds', 300))
                            job.defer(delay, type(error).__name__)
                            return False
                        if job.read()['attempts'] < self.settings.max_attempts:
                            return False
                        job.save('failed', error_type=type(error).__name__)
                    else:
                        # A storage failure must propagate as delivery failure, not a model error.
                        job.save('result_ready', result=result)
            row = job.read()
            try:
                # Callback retries do not rerun successful model work. 409 is surfaced
                # and retried for operator investigation; never overwrite a terminal result.
                self.pipeline.content.report(event['documentId'], event['documentVersion'],
                                             'failed' if row['state'] == 'failed' else 'succeeded')
            except DocumentUnavailable:
                job.save('skipped')  # discard derived data for unavailable documents
                return True
            if row['state'] == 'failed':
                job.save('completed', error_type=row['error_type'])
            else:
                self.publisher(self.settings, row)
                job.save('completed')  # clear staged result after confirmed delivery
            return True


def run_consumer(settings, handler):
    """Keep AMQP I/O on this thread and execute slow model work in one worker."""
    import time
    parameters = pika.URLParameters(settings.amqp_url)
    parameters.heartbeat = 60
    parameters.blocked_connection_timeout = 15
    with ThreadPoolExecutor(max_workers=1) as executor:
        while True:
            try:
                with pika.BlockingConnection(parameters) as connection:
                    channel = connection.channel()
                    channel.exchange_declare(exchange='document.events', exchange_type='topic', durable=True)
                    channel.queue_declare(queue='document.jobs', durable=True)
                    channel.queue_bind(queue='document.jobs', exchange='document.events', routing_key='document.uploaded')
                    channel.queue_declare(queue='processing.invalid-events', durable=True)
                    # Confirm invalid-message forwarding before acknowledging its source.
                    channel.confirm_delivery()
                    channel.basic_qos(prefetch_count=1)
                    logger.info('AI Processing consumer ready')
                    while True:
                        method, properties, body = channel.basic_get(queue='document.jobs', auto_ack=False)
                        if method is None:
                            connection.sleep(1)
                            continue
                        try:
                            event = parse_event(body)
                        except (ValueError, TypeError, KeyError, UnicodeError):
                            channel.basic_publish(exchange='', routing_key='processing.invalid-events',
                                body=body, mandatory=True,
                                properties=pika.BasicProperties(delivery_mode=2, content_type='application/json'))
                            channel.basic_ack(method.delivery_tag)
                            logger.warning('Malformed event moved to processing.invalid-events')
                            continue
                        # Slow HTTP/model work must not prevent AMQP heartbeats.
                        future = executor.submit(handler.handle, event)
                        while not future.done():
                            connection.sleep(0.2)
                        try:
                            done = future.result()
                        except Exception as error:
                            logger.warning('Job delivery deferred: %s', type(error).__name__)
                            done = False
                        if done:
                            # Successful return means the durable handoff has completed.
                            channel.basic_ack(method.delivery_tag)
                        else:
                            connection.sleep(3)
                            channel.basic_nack(method.delivery_tag, requeue=True)
            except pika.exceptions.AMQPError as error:
                logger.warning('Broker reconnect: %s', type(error).__name__)
                time.sleep(3)
