"""Tests for DELETE /api/trades/{id} and /api/disciplines endpoints."""

import uuid
import pytest
import os
os.environ.setdefault("CORS_ALLOW_ORIGINS", "https://trading-journal-test.vercel.app")

import main


def test_discipline_create_trims_whitespace():
    payload = main.DisciplineCreate(name="  Followed plan  ")
    assert payload.name == "Followed plan"


def test_discipline_create_rejects_empty_name():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        main.DisciplineCreate(name="   ")


def test_discipline_out_model_validation():
    d_id = uuid.uuid4()
    d_out = main.DisciplineOut(id=d_id, name="Hard Stop", created_at=None)
    assert d_out.id == d_id
    assert d_out.name == "Hard Stop"
