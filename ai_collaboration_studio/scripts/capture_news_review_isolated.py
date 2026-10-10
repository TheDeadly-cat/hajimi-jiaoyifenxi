"""Capture one local launcher without retrieving or persisting credentials."""
import argparse
import contextlib
import os
import runpy
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--run-root', required=True)
parser.add_argument('--mode', required=True, choices=('check', 'launch'))
args = parser.parse_args()
root = Path(args.run_root)
if not root.is_absolute():
    raise SystemExit('absolute_run_root_required')
helper = root / 'launch_news_review_isolated.py'
flag = '--check-boundary-only' if args.mode == 'check' else '--launch'
sys.argv = [str(helper), '--run-root', str(root), flag]
suffix = '-' + str(os.getpid()) if args.mode == 'check' else ''
with (root / ('isolated-launcher-' + args.mode + suffix + '.stdout.txt')).open('x', encoding='utf-8') as output, \
        (root / ('isolated-launcher-' + args.mode + suffix + '.stderr.txt')).open('x', encoding='utf-8') as errors:
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        try:
            runpy.run_path(str(helper), run_name='__main__')
        finally:
            for stream in (output, errors):
                stream.flush()
                os.fsync(stream.fileno())
