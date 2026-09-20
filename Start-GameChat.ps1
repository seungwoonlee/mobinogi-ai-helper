$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
& python (Join-Path $scriptRoot 'mabinogi_chat.py') @args
exit $LASTEXITCODE
