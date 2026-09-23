"""Drain subprocess output continuously into bounded rotating diagnostic files."""
import logging
import os
from logging.handlers import RotatingFileHandler
import subprocess
import threading


def start_logged(command, path, *, max_bytes=5*1024*1024, backups=4, **kwargs):
    handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    kwargs['env'] = {**os.environ, **kwargs.get('env', {}), 'PYTHONUTF8': '1', 'PYTHONUNBUFFERED': '1'}
    try:
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
    except BaseException:
        handler.close()
        raise

    def drain():
        try:
            while True:
                chunk = child.stdout.readline(8192)
                if not chunk:
                    break
                record = logging.LogRecord('process', logging.INFO, '', 0, chunk.decode('utf-8', errors='replace').rstrip(), (), None)
                handler.handle(record)
        finally:
            child.stdout.close()
            handler.close()
    threading.Thread(target=drain, daemon=True, name='log-' + str(child.pid)).start()
    return child
