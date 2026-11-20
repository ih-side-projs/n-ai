"""Test configuration for nai.spec tests."""

from __future__ import annotations

import sys
from pathlib import Path
import pytest

# Add WP4 to path
WP4_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WP4_ROOT))


@pytest.fixture
def sample_contract() -> dict:
    """Sample contract for testing."""
    return {
        "entities": [
            {"name": "User", "fields": [{"name": "id", "type": "int"}, {"name": "name", "type": "str"}]},
            {"name": "Order", "fields": [{"name": "id", "type": "int"}, {"name": "user_id", "type": "int"}, {"name": "total", "type": "float"}]},
        ],
        "components": [
            {"name": "auth", "provides": [{"name": "login", "signature": "login(username: str, password: str) -> User"}], "consumes": []},
            {"name": "orders", "provides": [{"name": "create_order", "signature": "create_order(user_id: int, items: list) -> Order"}], "consumes": ["auth"]},
        ],
        "scenarios": [
            {"id": "S1", "title": "User places order", "steps": [
                {"actor": "auth", "action": "login('alice', 'secret')", "expects": "User.id", "provides": ["User.id"]},
                {"actor": "orders", "action": "create_order(User.id, [...])", "expects": "Order.id", "provides": ["Order.id"]},
            ]},
        ],
        "external_inputs": [{"name": "credentials", "description": "username and password", "source": "user"}],
        "external_outputs": [{"name": "order_confirmation", "description": "order details", "destination": "user"}],
    }


@pytest.fixture
def sample_spec_markdown() -> str:
    """Sample spec markdown for testing."""
    return """# Component: orders

## Purpose
Manages customer orders and order processing.

## Interface
Provides: create_order(user_id: int, items: list) -> Order
Consumes: auth

## Requirements
- R1: Create order with valid user and items
- R2: Validate user exists before creating order
- R3: Calculate order total from item prices

## Edge Cases
- E1: User not found
- E2: Empty item list

## Example
Input: user_id=1, items=[{"product": "widget", "qty": 2, "price": 10.0}]
Output: Order(id=42, user_id=1, total=20.0)

## Dependencies
- auth
"""


@pytest.fixture
def sample_component_map() -> dict:
    """Sample component map for testing."""
    return {
        "components": [
            {"name": "auth", "objective": "Handle user authentication and sessions", "depends_on": []},
            {"name": "orders", "objective": "Manage customer orders and processing", "depends_on": ["auth"]},
            {"name": "inventory", "objective": "Track product inventory and stock levels", "depends_on": []},
        ],
        "shared_entities": [
            {"name": "User", "description": "Registered user", "fields": [{"name": "id", "type": "int"}, {"name": "email", "type": "str"}]},
            {"name": "Product", "description": "Product in catalog", "fields": [{"name": "id", "type": "int"}, {"name": "name", "type": "str"}, {"name": "price", "type": "float"}]},
        ],
        "external_inputs": [
            {"name": "credentials", "description": "User login credentials", "source": "user"},
        ],
        "external_outputs": [
            {"name": "order_confirmation", "description": "Order confirmation details", "destination": "user"},
        ],
    }