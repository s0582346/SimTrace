# Run the test suite and open a pytest-html-reporter dashboard (charts + summary tiles).
# Works around Smart App Control (uv trampolines blocked, os error 4551) by driving the
# real uv-managed base interpreter directly and loading the venv via site.addsitedir.
#
# NOTE: pytest-html-reporter 0.2.9 is unmaintained and assumes a private pytest attribute
# (_sessionstarttime) that newer pytest renamed. The _Shim plugin below restores it so the
# report generates on pytest 9.x. Kept in this launcher so the repo/test code stays clean.
#
# Any extra pytest args pass through, e.g.:
#   .\test-dashboard.ps1                                 # whole suite
#   .\test-dashboard.ps1 tests/test_tools/test_nodes.py  # subset
#   .\test-dashboard.ps1 -k source                       # by keyword
$py  = "C:\Users\paolo\AppData\Roaming\uv\python\cpython-3.11-windows-x86_64-none\python.exe"
$sp  = "C:\local\htw\simgen\.venv\Lib\site-packages"
$out = "C:/local/htw/simgen/test-dashboard.html"
$base = @('pytest', "--html-report=$out", '--title=simgen tests')
$pyArgs = $base + $args
$argv = ($pyArgs | ForEach-Object { "r'" + ($_ -replace "'","\'") + "'" }) -join ','
$code = @"
import site, sys, time
site.addsitedir(r'$sp')
import pytest
class _Shim:
    def pytest_sessionstart(self, session):
        tr = session.config.pluginmanager.get_plugin('terminalreporter')
        if tr is not None and not hasattr(tr, '_sessionstarttime'):
            tr._sessionstarttime = time.time()
sys.argv = [$argv]
sys.exit(pytest.main(sys.argv[1:], plugins=[_Shim()]))
"@
& $py -c $code
$rc = $LASTEXITCODE
if (Test-Path $out) { Invoke-Item $out }   # open dashboard regardless of pass/fail
exit $rc
