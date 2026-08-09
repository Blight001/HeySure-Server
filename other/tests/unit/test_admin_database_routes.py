from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from gateway.routers import admin_database_routes


def test_admin_database_router_exposes_browser_crud_paths():
    paths = {route.path for route in admin_database_routes.router.routes}
    assert paths == {
        "/db/tables",
        "/db/tables/{name}/rows",
        "/db/tables/{name}/rows/delete",
    }


@pytest.mark.parametrize(
    ("python_type", "raw", "expected"),
    [(int, "42", 42), (float, "2.5", 2.5), (bool, "yes", True), (str, "x", "x")],
)
def test_coerce_value_preserves_row_editor_contract(python_type, raw, expected):
    column = SimpleNamespace(
        name="value",
        type=SimpleNamespace(python_type=python_type),
    )
    assert admin_database_routes.coerce_value(column, raw) == expected


def test_coerce_value_reports_invalid_integer():
    column = SimpleNamespace(
        name="count",
        type=SimpleNamespace(python_type=int),
    )
    with pytest.raises(HTTPException) as caught:
        admin_database_routes.coerce_value(column, "not-an-int")
    assert caught.value.status_code == 400
    assert "count" in caught.value.detail
