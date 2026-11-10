# Synthetic fixture

`calc-src/` holds the source of a tiny Python project whose `add()` is deliberately broken.
`oh_my_grok.fixture.make_repo()` turns it into a disposable git repository (deterministic commit,
fixed author/date) per fixture job under `jobs/<job_id>/fixture-repo` (or under a test temp dir).

Verifier profile `fixture-calc-unittest` (config/verifiers.json) runs
`/workspace/omg/.venv/bin/python -m unittest discover -s tests -t . -v` with `shell=False` and treats
`tests/` as a protected path, so the only way to pass is to really fix `calc/__init__.py`. Printing
`VERIFIED` or `PASS` changes nothing: only the exit status is checked.
