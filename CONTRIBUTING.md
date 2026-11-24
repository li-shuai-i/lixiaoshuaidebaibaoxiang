# Contributing to csvscope

Thanks for taking a look. This project is intentionally small, and it stays
healthy by keeping three rules.

## 1. No dependencies

`csvscope` ships with **zero** third-party dependencies. Pull requests that add
`pandas`, `numpy`, `fastapi`, a JS framework or a bundler will be declined — the
"clone and run" promise is the whole point. The standard library is enough.

## 2. Tests come with the change

```bash
python -m unittest discover -s tests -v
```

- Engine changes go in `tests/test_core.py`.
- Route changes go in `tests/test_server.py` (it boots a real server on an
  ephemeral port).
- Bug fixes should include the case that used to fail.

CI runs the suite on Python 3.9, 3.11 and 3.13 across Linux, macOS and Windows.

## 3. Keep the surface small

Before adding a feature, ask whether it belongs in a data explorer. Joins, SQL
and write-back are out of scope on purpose. Adding a new filter operator or
aggregation is usually fine — adding a new architectural layer usually is not.

## Local workflow

```bash
git clone https://github.com/USERNAME/csvscope.git
cd csvscope
python -m csvscope --verbose     # http://127.0.0.1:8787/?sample
```

The frontend is plain HTML/CSS/JS — edit files in `csvscope/static/` and hit
reload. There is no build step and no watch mode to configure.

## Style

- Python: PEP 8, type hints on public functions, docstrings that explain *why*.
- JavaScript: no classes, no framework, one function per job; keep `app.js`
  readable top to bottom.
- CSS: custom properties for every colour so dark mode keeps working.

## Reporting bugs

Include: the command you ran, your Python version and OS, and — if possible — the
smallest CSV that reproduces the problem (3 rows is plenty). Please do not paste
real customer data; reduce it first.
