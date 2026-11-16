"""Explicit commands; imports and help do not initialize SQL or Kafka."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['initialize', 'worker', 'status', 'initialize-aml', 'aml-worker', 'aml-status'])
    args = parser.parse_args()
    from src.lab.config import load_lab_settings
    from src.lab.database import LabDatabase, LabDatabaseError
    from src.lab.kafka.broker import BrokerUnavailable, KafkaBroker
    from src.lab.kafka.consumer import run_worker
    from src.lab.kafka.store import KafkaStore
    try:
        settings = load_lab_settings()
        database = LabDatabase(settings)
        store = KafkaStore(database)
        if args.command in ('initialize-aml', 'aml-worker', 'aml-status'):
            from src.lab.kafka.analysis_store import AnalysisStore
            analyses = AnalysisStore(database)
            if args.command == 'initialize-aml':
                store.initialize()
                print(json.dumps(analyses.initialize()))
                return 0
            analyses.list_results(limit=1)
            if args.command == 'aml-status':
                print(json.dumps({'status': 'ready', 'queue': 'kafka_lab.analysis_requests'}))
                return 0
            from src.lab.kafka.analysis_consumer import run_worker as run_aml_worker
            from src.workflow.remote import WorkflowClient
            broker = KafkaBroker(worker=True, flow='aml')
            try:
                broker.status()
            except Exception:
                broker.close()
                raise
            return run_aml_worker(analyses, broker, WorkflowClient(settings=settings))
        if args.command == 'initialize':
            print(json.dumps(store.initialize()))
        elif args.command == 'status':
            print(json.dumps(store.status()))
        else:
            # Fail before joining the group when schema/dependencies are absent.
            store.status()
            broker = KafkaBroker(worker=True)
            try:
                broker.status()
            except Exception:
                broker.close()
                raise
            return run_worker(store, broker)
        return 0
    except (LabDatabaseError, BrokerUnavailable) as exc:
        print(json.dumps({'status': 'failed', 'code': exc.code}))
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception:
        print(json.dumps({'status': 'failed', 'code': 'KAFKA_LAB_COMMAND_FAILED'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
