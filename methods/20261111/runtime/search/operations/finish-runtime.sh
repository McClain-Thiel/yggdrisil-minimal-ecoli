#!/bin/bash
set -euo pipefail
R=/home/ubuntu/yggdrisil-baseline-depth-20261110
cd "$R"
exec 9>runtime-install.lock
flock -n 9
[ -f bootstrap.ready ]
[ ! -f runtime.ready ]
env/bin/uv pip install --python env/bin/python -c runtime-constraints.txt --override runtime-overrides.txt 'google-adk==2.13.0' 'litellm[mcp]==1.106.4' 'mcp==2.3.0' 'pandas==2.3.3' 'cobra==0.32.1' 'RBApy==3.0.3' 'RBAtools==2.0.1' 'setuptools==80.10.2' boto3 wandb pydantic pyarrow lark boolean.py pytest pytest-asyncio biopython aiosqlite networkx xxhash python-dotenv httpx
env/bin/uv pip install --python env/bin/python --no-deps -e framework -e application
env/bin/python operations/restore-inputs.py
env/bin/uv pip freeze --python env/bin/python > environment-freeze.txt
touch runtime.ready
