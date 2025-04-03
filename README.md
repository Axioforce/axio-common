# axio-common
Shared models and utilities for AxioForce's backend services.

This package includes:

- **SQLAlchemy models** used by both the API server and Streamlit dashboard.
- **Utility functions** for common operations such as timestamp formatting, hostname resolution, and client state tracking.

## Project Structure

```bash
axio-common/
├── README.md
├── pyproject.toml
├── __init__.py
├── models/
│   ├── __init__.py
│   ├── client.py
│   ├── device.py
│   └── ...
└── utils/
    ├── __init__.py
    └── ...
```

## Usage

In either the server or dashboard project:

```python
from axio_common.models.client import Client
from axio_common.utils.shared import resolve_hostname
```

## Installation (editable mode during development)

```bash
pip install -e /path/to/axio-common
```

## Notes

- Models assume the database is managed externally (e.g., via Alembic in the API repo).
- This project should not depend on FastAPI or Streamlit directly — it's meant to be a backend-agnostic shared core.
