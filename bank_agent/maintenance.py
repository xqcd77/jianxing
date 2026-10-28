"""Explicit local storage maintenance; status opens SQLite read-only."""
import argparse
import json
import sqlite3

from .config import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command',choices=['status','migrate','rebuild-memory','organize-memory'])
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.command=='status':
        path = settings.data_dir/'bank.sqlite3'
        if not path.is_file():
            print(json.dumps({'exists':False}))
            return
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            tasks = dict(conn.execute('SELECT status,count(*) FROM chat_tasks GROUP BY status')) if 'chat_tasks' in tables else {}
            migrations = [r[0] for r in conn.execute("SELECT id FROM documents WHERE kind='meta' AND id LIKE 'migration_%'")]
        print(json.dumps({'exists':True,'chat_tasks':tasks,'migrations':migrations},ensure_ascii=False))
        return
    from .storage import Database
    from .migrations import migrate_goal_state
    db = Database(settings.data_dir)
    try:
        migrate_goal_state(db)
        if args.command in {'rebuild-memory', 'organize-memory'}:
            from .memory import SharedMemory
            from .operations import BankService
            memory = SharedMemory(db,BankService(db),settings)
            try:
                if args.command == 'organize-memory':
                    memory.curator.bootstrap()
                    result = memory.curator.run_pending()
                else:
                    result = memory.rebuild_index()
            finally:
                memory.close()
        else:
            result = {'migration':'request_runtime_v1','status':'applied_or_already_current'}
        print(json.dumps(result,ensure_ascii=False))
    finally:
        db.engine.dispose()


if __name__=='__main__':
    main()
