#!/usr/bin/env bash
# Prove the Hyper-V console path as far as it can be proved without Windows.
#
# The unit tests assert what the protocol module builds. This asserts that what it builds
# is accepted by a real guacd: the handshake completes, guacd allocates a connection, and
# the failure to reach a Virtual Machine Connection endpoint comes back as the status this
# product translates for the operator.
#
# What this cannot stand in for is the Hyper-V host. Nothing here runs Windows, so nothing
# here proves that VMConnect accepts security=vmconnect with a GUID in the pre-connection
# blob. docs/hyperv-console.md names that measurement and what it is still owed.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
GUACD_PORT="${HYPERV_GUACD_PORT:-14822}"
PYTHON="${PYTHON:-python3}"

usage() {
    cat <<USAGE
Usage: ${0##*/}

Runs the console proof:

  1. starts guacd
  2. completes the Guacamole handshake with the product's own parameters
  3. asserts the VMConnect failure is reported with the product's own wording

Environment:
  HYPERV_GUACD_PORT   Port guacd listens on (default: 14822)
USAGE
}

[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && { usage; exit 0; }

echo "Starting guacd..."
(cd "$HERE" && docker compose up -d guacd >/dev/null)

echo "Waiting for guacd..."
for _ in $(seq 1 30); do
    if (echo > "/dev/tcp/127.0.0.1/$GUACD_PORT") >/dev/null 2>&1; then break; fi
    sleep 0.5
done

VERSION="$(docker logs pegaprox-hyperv-guacd 2>&1 | sed -n 's/.*guacd) version \([0-9.]*\).*/\1/p' | head -1)"
echo "  guacd version: ${VERSION:-unknown}"

"$PYTHON" - <<PY
import sys
sys.path.insert(0, "$REPO")
from pegaprox.core import hyperv_console as console

endpoint = ('127.0.0.1', $GUACD_PORT)
params = console.connection_parameters(
    host='127.0.0.1', vm_guid='11111111-1111-1111-1111-111111111111',
    username='console-testbed', password='console-testbed-only')

assert params['security'] == 'vmconnect', params['security']
assert params['port'] == '2179', params['port']

try:
    sock, reader, ready = console.connect(params, endpoint=endpoint)
except console.ConsoleError as exc:
    print(f'  FAIL: the handshake did not complete: {exc.message} {exc.detail}')
    raise SystemExit(1)

connection_id = ready[1] if len(ready) > 1 else ''
print(f'  handshake complete; guacd allocated a connection ({len(connection_id)} chars)')

sock.settimeout(20)
seen = []
status = None
while True:
    chunk = sock.recv(65536)
    if not chunk:
        break
    stop = False
    for raw in reader.feed(chunk):
        elements = console.decode(raw)
        seen.append(elements[0])
        if elements[0] == 'error':
            status = elements[2] if len(elements) > 2 else ''
            print(f'  guacd reported: {elements[1]!r} (status {status})')
            print(f'  PegaProx says:  {console.status_meaning(status)}')
            stop = True
    if stop:
        break
sock.close()

if status is None:
    print('  FAIL: guacd never reported the unreachable VMConnect endpoint')
    raise SystemExit(1)
if console.status_meaning(status) == 'The console could not be opened.':
    print(f'  FAIL: status {status} has no translation; add one rather than shipping the '
          f'generic message')
    raise SystemExit(1)
print('  the failure is reported in words an operator can act on')
PY

echo "Relaying that same connection over a websocket..."
# The gate in front of the relay is proven in tests/test_hyperv_console.py against the real
# access-control layer. What is proven here is the other half: that the relay carries
# guacd's stream to a browser unchanged, including the failure. So the authorization step
# is answered directly, and everything after it is the product's own code.
GUACD_PORT="$GUACD_PORT" REPO="$REPO" "$PYTHON" "$HERE/relay_check.py"

echo "Console path verified as far as Windows is not needed."
