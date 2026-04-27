# Contributing

Thanks for your interest. This is a portfolio project, but PRs and issues that
improve correctness, coverage, or documentation are welcome.

## Development setup

```bash
git clone https://github.com/Jathin21/Network-Automation-Framework
cd network-automation-framework
pip install -e ".[dev]"
```

## Workflow

1. Branch from `main`: `git checkout -b feature/short-description`
2. Add tests **before** code where practical.
3. Keep coverage at or above the current 93%.
4. Run the full check suite before pushing:
   ```bash
   ruff check netauto tests
   mypy netauto
   pytest tests/unit
   ```
5. Write a clear PR description: what, why, how it was tested.

## Adding a compliance rule

Add a new entry to `policies/baseline.yml` (or create a separate policy file).
Rule schema is defined in `netauto/validators/compliance.py::ComplianceRule`.

A rule with an empty `platforms:` list applies to every device. Otherwise, list
the platforms it applies to. Both `must_match` and `must_not_match` patterns
are compiled with `re.MULTILINE`.

Add a unit test in `tests/unit/test_compliance.py`.

## Adding a vendor

To add a new platform:

1. Add the value to `netauto.inventory.models.Platform`.
2. Add the NAPALM driver mapping to `Platform.napalm_driver`.
3. Add command mappings to `netauto.validators.change.ChangeValidator._COMMANDS`.
4. Add a section to each Jinja2 template under `configs/templates/`.
5. Add tests covering the new dispatch.

## Code style

- `ruff` configured in `pyproject.toml` is the source of truth.
- Type-annotate every public function. `mypy --strict` should pass.
- Prefer pure functions and explicit dependency injection (the `factory`
  pattern) over global state — it keeps tests trivial.

## Reporting bugs

Include:
- `netauto --version`
- Python version
- Vendor + OS version of the device(s) involved (if any)
- A minimal reproduction (an inventory snippet plus the command you ran)
- The full traceback or output

For network parser issues, please attach the raw CLI output the parser was
given — most parser bugs are about real-world output we haven't seen yet.
