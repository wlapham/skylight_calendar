"""Offline tests for the Up for grabs chore list; no credentials or live calls.

The real todo classes and helpers are loaded from the integration's AST. Home
Assistant's todo types and the coordinator base are small test doubles.
Run from the repository root: python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum, IntFlag
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import unittest

ROOT = Path(__file__).resolve().parents[1]


class TodoItemStatus(str, Enum):
    NEEDS_ACTION = "needs_action"
    COMPLETED = "completed"


class TodoListEntityFeature(IntFlag):
    CREATE_TODO_ITEM = 1
    DELETE_TODO_ITEM = 2
    UPDATE_TODO_ITEM = 4
    SET_DUE_DATE_ON_ITEM = 16
    SET_DESCRIPTION_ON_ITEM = 64


@dataclass
class TodoItem:
    summary: str | None = None
    uid: str | None = None
    status: TodoItemStatus | None = None
    due: date | datetime | None = None
    description: str | None = None


class CoordinatorEntity:
    def __class_getitem__(cls, _):
        return cls

    def __init__(self, coordinator):
        self.coordinator = coordinator


class TodoListEntity:
    pass


class HomeAssistantError(Exception):
    pass


def load_todo():
    tree = ast.parse((ROOT / "custom_components/skylight/todo.py").read_text(encoding="utf-8"))
    wanted = {"SkylightMemberChoreTodo", "SkylightUpForGrabsTodo",
              "_chore_is_complete", "_split_chore_uid", "_chore_start"}
    nodes = [n for n in tree.body
             if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name in wanted]
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[]))
    scope = {
        "TodoItem": TodoItem, "TodoItemStatus": TodoItemStatus,
        "TodoListEntity": TodoListEntity, "TodoListEntityFeature": TodoListEntityFeature,
        "CoordinatorEntity": CoordinatorEntity, "DeviceInfo": dict,
        "HomeAssistantError": HomeAssistantError,
        "SkylightSensorCoordinator": object,
        "DOMAIN": "skylight", "CHORE_COMPLETE_STATUSES": {"complete", "completed"},
        "dt_util": NS(now=lambda: datetime(2026, 11, 16, 18, 0)),
        "date": date, "datetime": datetime,
    }
    exec(compile(module, "<real todo platform>", "exec"), scope)
    return scope


MEMBER = "1001"


def chore(cid, summary, *, up_for_grabs, category=None, completed_by=None,
          status="pending", start="2026-11-16", completed_on=None):
    return {
        "id": cid, "type": "chore",
        "attributes": {"summary": summary, "status": status, "start": start,
                       "up_for_grabs": up_for_grabs, "completed_on": completed_on,
                       "description": None},
        "relationships": {
            "category": {"data": {"id": category, "type": "category"} if category else None},
            "completed_category": {"data": {"id": completed_by, "type": "category"} if completed_by else None},
        },
    }


# Shapes read from a live frame on 2026-11-16 (IDs and names here are made up): an up-for-grabs chore has no
# category; once grabbed and ticked, only completed_category names the person.
FEED = {"chores": {"data": [
    chore("2001", "Replace furnace filter", up_for_grabs=True, status="complete",
          completed_by=MEMBER, completed_on="2026-11-16"),
    chore("2002", "Clean dryer vent", up_for_grabs=True, start="2026-11-18"),
    chore("3001-2026-11-16-0600", "Water the plants", up_for_grabs=False, category=MEMBER),
]}}


class UpForGrabsTodoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.s = load_todo()
        self.coord = NS(data=FEED, async_request_refresh=AsyncMock())
        self.api = NS(create_chores=AsyncMock(), complete_chore=AsyncMock(),
                      uncomplete_chore=AsyncMock(), update_chore=AsyncMock(),
                      delete_chore=AsyncMock())
        self.grabs = self.s["SkylightUpForGrabsTodo"](self.coord, self.api, "9001", "Test Frame")

    def test_lists_only_up_for_grabs_chores_with_due_day_and_status(self):
        items = {i.summary: i for i in self.grabs.todo_items}
        self.assertEqual(set(items), {"Replace furnace filter", "Clean dryer vent"})
        self.assertEqual(items["Replace furnace filter"].status, TodoItemStatus.COMPLETED)
        self.assertEqual(items["Clean dryer vent"].status, TodoItemStatus.NEEDS_ACTION)
        self.assertEqual(items["Clean dryer vent"].due, date(2026, 11, 18))

    def test_grabbed_chore_stays_off_the_grabbers_own_list(self):
        member = self.s["SkylightMemberChoreTodo"](self.coord, self.api, "9001", "Test Frame", MEMBER, "Alex")
        self.assertEqual([i.summary for i in member.todo_items], ["Water the plants"])

    async def test_adding_an_item_creates_an_unassigned_up_for_grabs_chore(self):
        await self.grabs.async_create_todo_item(TodoItem(summary="Change HVAC filter", due=date(2026, 11, 20)))
        self.api.create_chores.assert_awaited_once()
        kwargs = self.api.create_chores.await_args.kwargs
        self.assertEqual(kwargs["summary"], "Change HVAC filter")
        self.assertEqual(kwargs["start"], "2026-11-20")
        self.assertEqual(kwargs["category_ids"], [])
        self.assertIs(kwargs["up_for_grabs"], True)

    async def test_ticking_an_item_is_refused_without_touching_the_frame(self):
        # Skylight answers 422 "category_id can't be blank": completing an up-for-grabs
        # chore must name who did it, and an HA tick has no "who". Ticks happen on the frame.
        with self.assertRaises(HomeAssistantError):
            await self.grabs.async_update_todo_item(
                TodoItem(uid="2002", summary="Clean dryer vent", status=TodoItemStatus.COMPLETED))
        self.api.complete_chore.assert_not_awaited()
        self.api.update_chore.assert_not_awaited()

    async def test_editing_an_item_still_updates_the_chore(self):
        await self.grabs.async_update_todo_item(
            TodoItem(uid="2002", summary="Clean the dryer vent", status=TodoItemStatus.NEEDS_ACTION))
        self.api.update_chore.assert_awaited_once()
        self.api.complete_chore.assert_not_awaited()

    def test_one_stable_entity_per_frame(self):
        self.assertEqual(self.grabs._attr_unique_id, "skylight_9001_chore_todo_up_for_grabs")
        self.assertEqual(self.grabs._attr_name, "Up for grabs")


if __name__ == "__main__":
    unittest.main()
