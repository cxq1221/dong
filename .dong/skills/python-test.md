# python-test

Always write tests before implementation (TDD when practical).

- Use `pytest` (not unittest) with plain `assert`
- Test file: `tests/test_<module>.py`
- One test function per scenario, descriptive names
- Cover: happy path, edge cases (empty/0/None), error cases
- Use `tmp_path` fixture for file operations, not hardcoded paths
- Run tests with: `python -m pytest tests/ -v`

Before submitting, verify:
1. `python -m pytest tests/ -v` passes
2. No new flaky tests (tests depend on order/global state)
