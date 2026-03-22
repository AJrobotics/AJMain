# Start agent in background (for SSH remote restart)
param([string]$Machine = "Gram")
$dir = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Start-Process -FilePath "python" -ArgumentList "-m","agent.start_agent","--machine",$Machine -WorkingDirectory $dir -WindowStyle Normal
