#!/bin/sh
echo "=== DIAGNOSTIC: Checking volume mounts ==="
echo "--- /opt/processor/venv/bin full listing ---"
ls -la /opt/processor/venv/bin/ 2>&1
echo "--- readlink python3 ---"
readlink -f /opt/processor/venv/bin/python3 2>&1 || echo "readlink failed"
echo "--- file python3.11 ---"
file /opt/processor/venv/bin/python3.11 2>&1 || echo "python3.11 not found"
echo "--- which python3.11 on host ---"
ls -la /opt/processor/venv/bin/python3.11 2>&1 || echo "MISSING python3.11 in venv"
echo "--- check system python ---"
ls -la /usr/bin/python3* 2>&1 || echo "no system python"
echo "--- .pyver ---"
cat /opt/processor/.pyver 2>&1
echo "=== END DIAGNOSTIC ==="

# Try multiple python paths
if [ -x /opt/processor/venv/bin/python3.11 ]; then
  exec /opt/processor/venv/bin/python3.11 /app/entrypoint.py "$@"
elif [ -x /opt/processor/venv/bin/python3 ]; then
  exec /opt/processor/venv/bin/python3 /app/entrypoint.py "$@"
else
  echo "FATAL: No python found. Trying readlink resolve..."
  REAL=$(readlink -f /opt/processor/venv/bin/python3 2>/dev/null)
  echo "Resolved: $REAL"
  ls -la "$REAL" 2>&1 || echo "Resolved path doesn't exist either"
  exit 1
fi
