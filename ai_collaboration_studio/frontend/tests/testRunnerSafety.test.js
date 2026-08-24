import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const packageJson = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
const runnerSource = readFileSync(new URL("../scripts/run-tests-safe.ps1", import.meta.url), "utf8");

test("all supported frontend test scripts route through the guarded runner", () => {
  const expected = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/run-tests-safe.ps1";
  assert.equal(packageJson.scripts.test, expected);
  assert.equal(packageJson.scripts["test:file"], expected);
});

test("the guarded runner monitors the actual test body and fails closed", () => {
  assert.match(runnerSource, /Parameter\(Position = 0, ValueFromRemainingArguments = \$true\)/);
  assert.match(runnerSource, /--max-old-space-size=2048/);
  assert.match(runnerSource, /--test-concurrency=1/);
  assert.match(runnerSource, /--test-isolation=none/);
  assert.match(runnerSource, /\[int\]\$MaxPrivateMemoryMB = 3072/);
  assert.match(runnerSource, /\[int\]\$TimeoutSeconds = 120/);
  assert.match(runnerSource, /\[int\]\$MaxOutputMB = 64/);
  assert.match(runnerSource, /RedirectStandardOutput = \$true/);
  assert.match(runnerSource, /BaseStream\.CopyToAsync/);
  assert.match(runnerSource, /GetProperty\('ArgumentList'\)/);
  assert.match(runnerSource, /\$argumentList\.Add\(\$argument\)/);
  assert.match(runnerSource, /\$startInfo\.Arguments = \$quotedArguments -join ' '/);
  assert.match(runnerSource, /\$argument\.Contains\('\"'\) -or \$argument\.EndsWith\('\\'\)/);
  assert.match(runnerSource, /Get-Content -LiteralPath \$stdoutPath -Encoding UTF8/);
  assert.match(runnerSource, /while \(-not \$process\.WaitForExit\(250\)\)/);
  assert.match(runnerSource, /\$process\.Refresh\(\)\s*\$exitCode = \$process\.ExitCode/);
  assert.match(runnerSource, /if \(-not \$completedNormally[\s\S]*Stop-TestProcessTree -Process \$process/);
  assert.doesNotMatch(runnerSource, /Get-CimInstance|Win32_Process/);
});
