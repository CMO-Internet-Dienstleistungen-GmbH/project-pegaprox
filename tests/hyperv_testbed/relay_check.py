"""Drive the console relay from a browser's side, against a real guacd.

Called by verify_console.sh, which starts guacd first. The authorization step is answered
directly here: what it decides is proven in tests/test_hyperv_console.py against the real
access-control layer, and what is proven here is the other half — that the relay carries
guacd's stream to a browser unchanged, including a failure.

Nothing here runs Windows, so nothing here proves that a Hyper-V host accepts the
connection. docs/hyperv-console.md names that measurement.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.environ.get('REPO', '.'))

from pegaprox.core import hyperv_console as console  # noqa: E402
from pegaprox.core import hyperv_console_ws as relay  # noqa: E402

GUACD_PORT = int(os.environ.get('GUACD_PORT', '14822'))
RELAY_PORT = int(os.environ.get('RELAY_PORT', '14823'))
GUID = '11111111-1111-1111-1111-111111111111'


def main():
    try:
        import websockets
    except ImportError:
        print('  SKIPPED: the websockets library is not installed in this interpreter')
        return 0

    parameters = console.connection_parameters(
        host='127.0.0.1', vm_guid=GUID,
        username='console-testbed', password='console-testbed-only')

    os.environ[console.GUACD_PORT_ENV] = str(GUACD_PORT)
    relay.authorize = lambda path: (
        {'cluster_id': 'hv_1', 'vmid': 100, 'username': 'testbed',
         'parameters': parameters}, None)

    if not relay.start_hyperv_console_server(RELAY_PORT, host='127.0.0.1'):
        print('  FAIL: the relay did not start')
        return 1

    async def drive():
        url = (f'ws://127.0.0.1:{RELAY_PORT}/api/hyperv/hv_1/vms/100/console'
               f'?token=testbed')
        async with websockets.connect(url, subprotocols=['guacamole']) as socket:
            seen = []
            while len(seen) < 60:
                message = await asyncio.wait_for(socket.recv(), timeout=25)
                elements = console.decode(message.rstrip(';'))
                seen.append(elements[0])
                if elements[0] == 'error':
                    status = elements[2] if len(elements) > 2 else '?'
                    print(f'  the browser received: {elements[1]!r} (status {status})')
                    return seen
            return seen

    try:
        seen = asyncio.run(drive())
    except Exception as exc:  # noqa: BLE001 — the failure is the result
        print(f'  FAIL: the relay did not answer: {type(exc).__name__}: {exc}')
        return 1

    if 'ready' not in seen:
        print(f'  FAIL: the browser never received a ready instruction: {seen[:10]}')
        return 1
    if 'error' not in seen:
        print(f'  FAIL: the failure never reached the browser: {seen[:10]}')
        return 1
    print('  the relay carried both the connection and its failure to the browser')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
