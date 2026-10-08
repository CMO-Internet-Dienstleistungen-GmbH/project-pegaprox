#!/usr/bin/env python3
"""Write the product's PowerShell scripts to disk so PowerShell can execute them.

The scripts live in pegaprox/core/hyperv_scripts.py as the single source. Exporting rather
than copying is what makes the fixtures evidence: what PowerShell runs here is byte for
byte what the product sends to a host.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from pegaprox.core import hyperv_scripts  # noqa: E402


def main() -> int:
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                       else pathlib.Path(__file__).parent / 'ps_fixtures' / '_scripts')
    out.mkdir(parents=True, exist_ok=True)
    for name, script in hyperv_scripts.ALL_SCRIPTS.items():
        (out / f'{name}.ps1').write_text(script)
    print(f'exported {len(hyperv_scripts.ALL_SCRIPTS)} scripts to {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
