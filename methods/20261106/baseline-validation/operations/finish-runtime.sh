#!/usr/bin/env bash
set -euo pipefail
ROOT=/home/ubuntu/yggdrisil-baseline-validation-20261106
test -e "$ROOT/bootstrap.ready"
cd "$ROOT"
"$ROOT/env/bin/uv" pip install --python "$ROOT/env/bin/python" -c runtime-constraints.txt boto3 wandb pydantic pandas pyarrow lark boolean.py pytest pytest-asyncio biopython aiosqlite networkx xxhash
"$ROOT/env/bin/uv" venv --python "$ROOT/native/.venv/bin/python" "$ROOT/build-env"
"$ROOT/env/bin/uv" pip install --python "$ROOT/build-env/bin/python" setuptools==80.10.2 wheel==0.46.3 Cython==3.1.2 numpy==2.2.6
cd "$ROOT/native"
"$ROOT/build-env/bin/python" setup.py build_ext --inplace
"$ROOT/native/.venv/bin/python" -c 'import wholecell.utils._build_sequences, wholecell.utils._fastsums, wholecell.utils.mc_complexation; print("Native extensions import successfully")'
test -z "$(git status --porcelain --untracked-files=no)"
git clone --shared "$ROOT/native" "$ROOT/native-validated"
cd "$ROOT/native-validated"
ln -s "$ROOT/native/.venv" .venv
git apply "$ROOT/solver-recovery-v2.patch"
cp "$ROOT"/native/wholecell/utils/*.so wholecell/utils/
export PYTHONPATH="$ROOT/application/src:$ROOT/application:$ROOT/framework/src"
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
export PYDANTIC_DISABLE_PLUGINS=__all__
cd "$ROOT"
"$ROOT/env/bin/python" -m pytest tests/test_experiment_tracking.py tests/test_continuation_validation_guards.py -q
PYTHONPATH="$ROOT/native-validated" "$ROOT/native/.venv/bin/python" -m pytest test-solver-recovery-v2.py -q -k 'not captured_failed_cell'
"$ROOT/env/bin/python" -u prepare-inputs.py
