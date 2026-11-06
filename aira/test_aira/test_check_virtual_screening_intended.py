# SPDX-FileCopyrightText: Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for check_virtual_screening_intended.

These tests mock the LLM, so they need neither a GPU nor any NIM / API key. They also run
offline: see _import_check_virtual_screening_intended.
"""

import logging
import sys
import types
from types import SimpleNamespace

import pytest

EVENT_KEY = "check_virtual_screening_intended"


def _import_check_virtual_screening_intended():
    """Import check_virtual_screening_intended without network access.

    aiq_aira.nodes does `from rcsbapi.search import TextQuery, AttributeQuery`, and importing
    rcsbapi.search downloads the RCSB search schema over the network. The function under test
    does not use rcsbapi, so when aiq_aira.nodes is not imported yet, import it with a
    throwaway rcsbapi stub and then drop the stub and that copy of aiq_aira.nodes from
    sys.modules, so any later import (e.g. by other tests) gets the real modules.
    """
    if "aiq_aira.nodes" in sys.modules:
        return sys.modules["aiq_aira.nodes"].check_virtual_screening_intended

    stub_names = [name for name in ("rcsbapi", "rcsbapi.search") if name not in sys.modules]
    stub_search = types.ModuleType("rcsbapi.search")
    stub_search.TextQuery = stub_search.AttributeQuery = object
    stubs = {"rcsbapi": types.ModuleType("rcsbapi"), "rcsbapi.search": stub_search}
    for name in stub_names:
        sys.modules[name] = stubs[name]
    try:
        import aiq_aira.nodes as nodes
    finally:
        for name in stub_names:
            sys.modules.pop(name, None)
        sys.modules.pop("aiq_aira.nodes", None)
        if hasattr(sys.modules.get("aiq_aira"), "nodes"):
            delattr(sys.modules["aiq_aira"], "nodes")
    return nodes.check_virtual_screening_intended


check_virtual_screening_intended = _import_check_virtual_screening_intended()


class FakeLLM:
    """Minimal stand-in for a LangChain chat model: ainvoke returns an object with .content."""

    def __init__(self, content: str):
        self.content = content

    async def ainvoke(self, _prompt):
        return SimpleNamespace(content=self.content)


async def _run(content: str):
    events = []
    result = await check_virtual_screening_intended(
        FakeLLM(content), events.append, "Introduction, Proposed small molecules", "cystic fibrosis")
    return result, events


@pytest.mark.asyncio
@pytest.mark.parametrize("content,expected", [
    ('```json\n{"intention": "yes"}\n```', True),
    ('{"intention": "Yes"}', True),
    ('```json\n{"intention": "no"}\n```', False),
])
async def test_valid_json_is_unchanged(content, expected, caplog):
    with caplog.at_level(logging.WARNING):
        result, events = await _run(content)
    assert result is expected
    assert events == [{EVENT_KEY: "Intention of virtual screening: " + ("yes" if expected else "no")}]
    assert not caplog.records


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    "Yes, virtual screening is intended for this report.",  # plain-text reply (issue #11)
    "yes",
    "  **Yes.** The report organization proposes novel small molecules.",
    '"Yes" - virtual screening would help here.',
])
async def test_plain_text_yes_is_treated_as_yes(content, caplog):
    with caplog.at_level(logging.WARNING):
        result, events = await _run(content)
    assert result is True
    assert len(events) == 1
    text = events[0][EVENT_KEY]
    assert text.startswith("Intention of virtual screening: yes")
    assert "could not be parsed" in text
    assert any(repr(content) in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    "No, virtual screening is not intended.",
    "no",
    "> **No** - the topic is not a disease.",
])
async def test_plain_text_no_is_treated_as_no(content, caplog):
    with caplog.at_level(logging.WARNING):
        result, events = await _run(content)
    assert result is False
    assert len(events) == 1
    text = events[0][EVENT_KEY]
    assert text.startswith("Intention of virtual screening: no")
    assert "could not be parsed" in text
    assert any(repr(content) in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    "I think virtual screening might help, yes.",  # yes/no not at the start: ambiguous
    "Yesterday's report suggests otherwise.",  # "yes" only as a word prefix
    "Not sure.",  # "no" only as a word prefix
    "asdf qwerty",  # gibberish
    "",  # empty reply
    '{"intention": ',  # truncated JSON
    '{"answer": "yes"}',  # valid JSON, missing the expected key
    '["yes"]',  # valid JSON, wrong shape
    '{"intention": null}',  # valid JSON, wrong value type
])
async def test_unparseable_reply_falls_back_to_not_intended(content, caplog):
    with caplog.at_level(logging.WARNING):
        result, events = await _run(content)
    # Must not raise, and must default to "virtual screening not intended".
    assert result is False
    assert len(events) == 1
    text = events[0][EVENT_KEY]
    assert text.startswith("Intention of virtual screening: no")
    assert "could not be parsed" in text
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("virtual screening" in m.lower() and repr(content) in m for m in warnings)


@pytest.mark.asyncio
async def test_logged_reply_is_truncated(caplog):
    content = "Maybe. " + "x" * 1000
    with caplog.at_level(logging.WARNING):
        result, _ = await _run(content)
    assert result is False
    messages = [r.getMessage() for r in caplog.records]
    assert any(repr(content[:200]) in m for m in messages)
    assert not any(content in m for m in messages)
