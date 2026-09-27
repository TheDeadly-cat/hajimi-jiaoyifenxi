"""One independent local watchdog check. No GET, DB, restart or notification send.

Schedule this separately only after the future observer is prepared/approved.
The exit status and saved alert receipt are the scheduler's notification input.
"""
import argparse
import ctypes
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.news_review_monitor import evaluate_watchdog, notification_changes, publish_json_once


def native_creation_ticks(pid):
    if os.name!='nt' or type(pid) is not int or pid<=0:
        return None
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
    kernel.GetProcessTimes.restype=wintypes.BOOL
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    kernel.WaitForSingleObject.restype=wintypes.DWORD
    handle=kernel.OpenProcess(0x101000,False,pid)
    if not handle:
        return None
    try:
        if kernel.WaitForSingleObject(handle,0)!=258:
            return None
        fields=[wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle,*[ctypes.byref(v) for v in fields]):
            return None
        return (fields[0].dwHighDateTime<<32)+fields[0].dwLowDateTime+504911232000000000
    finally:
        kernel.CloseHandle(handle)


def read_receipt_chain(directory, identity):
    previous=''
    latest=None
    paths=sorted(Path(directory).glob('monitor-execution-*.json'))
    if len(paths)>10000:
        raise ValueError('receipt_count_limit')
    for sequence,path in enumerate(paths,1):
        if path.stat().st_size>65536:
            raise ValueError('receipt_size_limit')
        wrapped=json.loads(path.read_text(encoding='utf-8'))
        r=wrapped['receipt']
        digest=hashlib.sha256(json.dumps(r,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if (wrapped['sha256']!=digest or r['identity']!=identity or r['sequence']!=sequence
                or r['previous_sha256']!=previous or path.name!=f'monitor-execution-{sequence:06d}.json'):
            raise ValueError('receipt_chain_unconfirmed')
        previous,latest=digest,r
    return latest


def check(directory, pin, previous_alerts, *, now_ms, inspect=native_creation_ticks):
    identity=pin['identity']
    expected=pin.get('process_start_utc_ticks')
    first,second=inspect(pin.get('pid')),inspect(pin.get('pid'))
    alive=type(expected) is int and expected>0 and first==second==expected
    integrity=False
    try:
        receipt=read_receipt_chain(directory,identity)
        integrity=receipt is not None
    except (ValueError,KeyError,OSError,TypeError):
        receipt=None
    verdict=evaluate_watchdog(receipt,identity=identity,now_ms=now_ms,observer_identity_alive=alive)
    if first!=second or first is not None and first!=expected:
        verdict['alerts'].append('OBSERVER_IDENTITY_UNCONFIRMED')
    verdict['notification_required']=bool(verdict['alerts'])
    verdict['receipt_chain_verified']=integrity
    verdict['changes']=notification_changes(previous_alerts,verdict)
    return verdict


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipts',required=True)
    parser.add_argument('--pin',required=True,help='Prepared PID, native UTC ticks and candidate/activation/policy identity JSON')
    parser.add_argument('--previous',help='Previous watchdog output for notification deduplication')
    parser.add_argument('--output',required=True,help='New unique JSON output; existing files are never overwritten')
    args=parser.parse_args()
    pin=json.loads(Path(args.pin).read_text(encoding='utf-8'))
    previous=json.loads(Path(args.previous).read_text(encoding='utf-8')) if args.previous else None
    if previous and previous['identity']!=pin['identity']:
        raise ValueError('previous_watchdog_identity_mismatch')
    verdict=check(args.receipts,pin,previous['alerts'] if previous else [],now_ms=time.time_ns()//1_000_000)
    publish_json_once(args.output, verdict)
    print(json.dumps({'output':args.output,'notify':verdict['changes']['notify'],'alerts':verdict['alerts']}))
    return 2 if verdict['changes']['notify'] else 0


if __name__=='__main__':
    raise SystemExit(main())
