"""Run with python main.py from ai-processing-service/."""
import argparse
import logging
from src.config import Settings


def main():
    parser = argparse.ArgumentParser(description='StudyHub AI Processing worker')
    parser.add_argument('--check-config', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        settings = Settings.load()
        settings.validate()
    except ValueError as error:
        parser.exit(2, f'Configuration error: {error}\n')
    if args.check_config:
        print('Configuration valid (network connectivity not checked)')
        return
    from src.clients.content_client import ContentClient
    from src.clients.model_client import ModelClient
    from src.job_store import JobStore
    from src.pipeline import Pipeline
    from src.consumer import Handler, run_consumer
    store = JobStore(settings.database_url)
    store.initialize()
    pipeline = Pipeline(settings, ContentClient(settings), ModelClient(settings))
    try:
        run_consumer(settings, Handler(settings, store, pipeline))
    except KeyboardInterrupt:
        logging.info('Worker stopped')


if __name__ == '__main__':
    main()
