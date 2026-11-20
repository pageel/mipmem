## Summary

Brief summary of the changes introduced in this pull request.

## Type of Change

- [ ] Bug fix (non-breaking change fixing an issue)
- [ ] New feature (non-breaking change adding functionality)
- [ ] Breaking change (fix or feature causing existing functionality to change)
- [ ] Documentation update
- [ ] CI / Refactoring / Code quality improvement

## Verification Checklist

- [ ] Zero external runtime dependencies preserved (`pyproject.toml` `dependencies = []`)
- [ ] `python3 -m py_compile` & `flake8 src scripts tests` passed with 0 errors
- [ ] `npx --yes pyright` passed with 0 errors and 0 warnings
- [ ] `python3 scripts/check_cleanroom.py` passed with 0 violations (no secrets, personal paths, or `.db` files)
- [ ] `python3 scripts/build_pyz.py` rebuilt cleanly and `bin/mipmem.pyz` verified (`< 25ms` fast-path preserved)
- [ ] `PYTHONPATH=src:. python3 -m unittest discover -s tests -v` passed cleanly
- [ ] Relevant documentation updated (`README.md`, `CHANGELOG.md`)

## Merge Strategy

- [ ] Merge using **Create a merge commit (`--merge`)** to preserve clean-room atomic commit history (do not squash).

## Related Issue / Milestone

Refs #
