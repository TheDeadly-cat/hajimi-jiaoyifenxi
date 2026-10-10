"""Execute the real PowerShell inspector with synthetic enumeration/listener inputs.

These are producer contract tests, not real CIM/Win32, HTTP, or trial evidence.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

APP = Path(__file__).resolve().parents[1]
INSPECTOR = APP / 'scripts/inspect_news_review_process_tree.ps1'
PWSH = os.environ.get('NEWS_REVIEW_TEST_PWSH') or shutil.which('pwsh') or shutil.which('pwsh.exe')
if not PWSH and os.environ.get('USERPROFILE'):
    bundled = Path(os.environ['USERPROFILE']) / '.cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell/pwsh.exe'
    if bundled.is_file():
        PWSH = str(bundled)

HOST = {'pid': 1001, 'parent_pid': 42, 'start_utc': '2026-10-11T01:00:00.1234567Z'}
CHILD = {'pid': 2001, 'parent_pid': 1001, 'start_utc': '2026-10-11T01:00:01.1234569Z'}
OTHER = {'pid': 9001, 'parent_pid': 42, 'start_utc': '2026-10-11T00:00:00.1234567Z'}

HARNESS = r'''
param([string]$FixturePath,[string]$InspectorPath)
$ErrorActionPreference = 'Stop'
$global:NewsInspectionCase = Get-Content -LiteralPath $FixturePath -Raw -Encoding utf8 | ConvertFrom-Json
$global:NewsInspectionCimIndex = 0
function Get-CimInstance {
    param([string]$Query)
    if ($Query -cne 'SELECT ProcessId,ParentProcessId,CreationDate FROM Win32_Process') { throw 'unexpected_cim_query' }
    $fixtureIndex = [Math]::Min($global:NewsInspectionCimIndex, $global:NewsInspectionCase.cim_snapshots.Count-1)
    $global:NewsInspectionCimIndex += 1
    foreach ($row in @($global:NewsInspectionCase.cim_snapshots[$fixtureIndex])) {
        $created = $null
        if ($null -ne $row.start_utc) {
            $fixtureStart = $(if ($row.start_utc -is [DateTime]) { $row.start_utc.ToUniversalTime() }
                else { ([DateTimeOffset]::Parse([string]$row.start_utc)).UtcDateTime })
            $fixtureTicks = $fixtureStart.Ticks
            $created = [DateTime]::new($fixtureTicks-($fixtureTicks % 10),[DateTimeKind]::Utc)
        }
        [pscustomobject]@{ProcessId=$row.pid;ParentProcessId=$row.parent_pid;CreationDate=$created}
    }
}
function Get-Process {
    foreach ($row in @($global:NewsInspectionCase.native)) {
        $started = $null
        if ($null -ne $row.start_utc) {
            $started = $(if ($row.start_utc -is [DateTime]) { $row.start_utc.ToUniversalTime() }
                else { ([DateTimeOffset]::Parse([string]$row.start_utc)).UtcDateTime })
        }
        [pscustomobject]@{Id=$row.pid;StartTime=$started}
    }
}
function Get-NetTCPConnection {
    param([string]$State)
    if ($State -cne 'Listen') { throw 'unexpected_listener_query' }
    foreach ($row in @($global:NewsInspectionCase.listeners)) { $row }
}
$fixtureInput = Join-Path (Split-Path -Parent $FixturePath) 'inspection-input.json'
$inputJson = $global:NewsInspectionCase.inspection_input | ConvertTo-Json -Depth 12 -Compress
[IO.File]::WriteAllText($fixtureInput,$inputJson,[Text.UTF8Encoding]::new($false))
& $InspectorPath -InputPath $fixtureInput
'''


def scenario(first=None, last=None, native=None, pins=None, listeners=None):
    first = [HOST] if first is None else first
    last = first if last is None else last
    native = last if native is None else native
    pin = {'pid': HOST['pid'], 'parent_pid': None, 'start_utc': HOST['start_utc']}
    pins = [pin] if pins is None else pins
    return copy.deepcopy({'cim_snapshots': [first, last], 'native': native,
        'listeners': [{'LocalAddress': '127.0.0.1', 'LocalPort': 49500, 'OwningProcess': HOST['pid']}] if listeners is None else listeners,
        'inspection_input': {'version': 'news_review_native_inspection_input_v1',
            'identity': {'candidate_sha': 'a'*40, 'activation_sha256': 'b'*64, 'policy_sha256': 'c'*64},
            'host_url': 'http://127.0.0.1:49500/', 'launcher_pin': pin,
            'host_pin': pin, 'pins': pins}})


@unittest.skipUnless(PWSH, 'PowerShell 7 unavailable; inspector producer path NOT_TESTED')
class NativeInspectionScopeTests(unittest.TestCase):
    def inspect(self, value):
        with tempfile.TemporaryDirectory(prefix='news-native-scope-') as directory:
            root = Path(directory)
            fixture = root/'fixture.json'
            fixture.write_text(json.dumps(value), encoding='utf-8')
            harness = root/'harness.ps1'
            harness.write_text(HARNESS, encoding='utf-8')
            process = subprocess.run([PWSH, '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(harness),
                '-FixturePath', str(fixture), '-InspectorPath', str(INSPECTOR)],
                capture_output=True, text=True, encoding='utf-8', timeout=20,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout)
        self.assertIs(result['database_opened'], False)
        self.assertEqual(result['http_requests'], 0)
        return result

    def test_stable_owner_and_discovered_child_preserve_native_precision(self):
        result = self.inspect(scenario(first=[HOST, CHILD]))
        self.assertIs(result['safe_status_reads_allowed'], True)
        children = [p for p in result['pins'] if p['pid'] == CHILD['pid']]
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]['start_utc'], CHILD['start_utc'])

    def test_unrelated_native_only_process_does_not_block_bound_owner(self):
        result = self.inspect(scenario(native=[HOST, OTHER]))
        self.assertIs(result['enumeration_consistent'], True)
        self.assertIs(result['safe_status_reads_allowed'], True)
        self.assertEqual([p['pid'] for p in result['pins']], [HOST['pid']])

    def test_unrelated_process_removal_does_not_block_bound_owner(self):
        result = self.inspect(scenario(first=[HOST, OTHER], last=[HOST], native=[HOST]))
        self.assertIs(result['enumeration_consistent'], True)
        self.assertIs(result['safe_status_reads_allowed'], True)

    def test_unrelated_generation_change_does_not_add_a_process_pin(self):
        other_new = {**OTHER, 'start_utc': '2026-10-11T01:00:02.1234567Z'}
        result = self.inspect(scenario(first=[HOST, OTHER], last=[HOST, other_new], native=[HOST, other_new]))
        self.assertIs(result['safe_status_reads_allowed'], True)
        self.assertEqual(len(result['pins']), 1)

    def test_related_child_birth_between_snapshots_remains_unconfirmed(self):
        result = self.inspect(scenario(first=[HOST], last=[HOST, CHILD], native=[HOST, CHILD]))
        self.assertIs(result['enumeration_consistent'], False)
        self.assertIs(result['safe_status_reads_allowed'], False)

    def test_related_child_disappearance_between_snapshots_remains_unconfirmed(self):
        result = self.inspect(scenario(first=[HOST, CHILD], last=[HOST], native=[HOST]))
        self.assertIs(result['enumeration_consistent'], False)
        self.assertIs(result['safe_status_reads_allowed'], False)

    def test_related_generation_change_between_snapshots_remains_unconfirmed(self):
        child_new = {**CHILD, 'start_utc': '2026-10-11T01:00:02.1234569Z'}
        result = self.inspect(scenario(first=[HOST, CHILD], last=[HOST, child_new], native=[HOST, child_new]))
        self.assertIs(result['enumeration_consistent'], False)
        self.assertIs(result['safe_status_reads_allowed'], False)

    def test_reused_owner_cannot_bind_new_descendants_to_old_generation(self):
        owner_new = {**HOST, 'start_utc': '2026-10-11T01:00:02.1234567Z'}
        child_new = {**CHILD, 'start_utc': '2026-10-11T01:00:03.1234569Z'}
        result = self.inspect(scenario(first=[owner_new, child_new]))
        self.assertIs(result['safe_status_reads_allowed'], False)
        self.assertIs(result['known_identity_inspection_incomplete'], True)
        self.assertIs(result['processes'][0]['pid_reused'], True)

    def test_owner_absent_from_cim_but_present_natively_is_not_declared_complete(self):
        result = self.inspect(scenario(first=[OTHER], last=[OTHER], native=[HOST, OTHER]))
        self.assertIs(result['enumeration_consistent'], False)
        self.assertIs(result['known_identity_inspection_incomplete'], True)
        self.assertIs(result['safe_status_reads_allowed'], False)

    def test_related_native_start_time_unavailable_remains_unconfirmed(self):
        result = self.inspect(scenario(first=[HOST, CHILD], native=[HOST, {**CHILD, 'start_utc': None}]))
        self.assertIs(result['known_identity_inspection_incomplete'], True)
        self.assertIs(result['safe_status_reads_allowed'], False)

    def test_related_reparenting_between_snapshots_remains_unconfirmed(self):
        orphan = {**CHILD, 'parent_pid': 42}
        result = self.inspect(scenario(first=[HOST, CHILD], last=[HOST, orphan], native=[HOST, orphan]))
        self.assertIs(result['enumeration_consistent'], False)
        self.assertIs(result['safe_status_reads_allowed'], False)

    def test_registered_child_is_not_dropped_when_no_longer_present(self):
        pin = {'pid': HOST['pid'], 'parent_pid': None, 'start_utc': HOST['start_utc']}
        result = self.inspect(scenario(pins=[pin, CHILD]))
        child = [p for p in result['processes'] if p['pid'] == CHILD['pid']]
        self.assertEqual(len(child), 1)
        self.assertIs(child[0]['alive'], False)

    def test_stable_parent_mismatch_remains_unconfirmed(self):
        pin = {'pid': HOST['pid'], 'parent_pid': None, 'start_utc': HOST['start_utc']}
        orphan = {**CHILD, 'parent_pid': 42}
        result = self.inspect(scenario(first=[HOST, orphan], pins=[pin, CHILD]))
        self.assertIs(result['known_identity_inspection_incomplete'], True)
        self.assertIs(result['safe_status_reads_allowed'], False)

    def test_wrong_listener_owner_and_nonloopback_address_remain_rejected(self):
        for listener in [{'LocalAddress': '127.0.0.1', 'LocalPort': 49500, 'OwningProcess': 9001},
                         {'LocalAddress': '0.0.0.0', 'LocalPort': 49500, 'OwningProcess': HOST['pid']}]:
            with self.subTest(listener=listener):
                result = self.inspect(scenario(listeners=[listener]))
                self.assertIs(result['safe_status_reads_allowed'], False)

    def test_related_identity_change_after_first_snapshot_is_not_ignored(self):
        owner_new = {**HOST, 'start_utc': '2026-10-11T01:00:02.1234567Z'}
        result = self.inspect(scenario(first=[HOST], last=[owner_new], native=[owner_new]))
        self.assertIs(result['enumeration_consistent'], False)
        self.assertIs(result['safe_status_reads_allowed'], False)

    def test_missing_owner_stays_not_alive_without_erasing_registered_identity(self):
        result = self.inspect(scenario(first=[OTHER], last=[OTHER], native=[OTHER]))
        self.assertIs(result['owner_alive'], False)
        self.assertIs(result['safe_status_reads_allowed'], False)
        self.assertEqual(result['pins'][0]['pid'], HOST['pid'])


if __name__ == '__main__':
    unittest.main()
