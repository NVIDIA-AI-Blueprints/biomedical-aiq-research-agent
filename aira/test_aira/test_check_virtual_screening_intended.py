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

These tests mock the LLM, so they need neither a GPU nor any NIM / API key.
"""

import logging
from types import SimpleNamespace

import pytest

from aiq_aira.nodes import check_virtual_screening_intended


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
async def test_valid_json_is_unchanged(content, expected):
    result, events = await _run(content)
    assert result is expected
    assert len(events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    "Yes, virtual screening is intended for this report.",  # plain-text reply (issue #11)
    "",  # empty reply
    '{"intention": ',  # truncated JSON
    '{"answer": "yes"}',  # valid JSON, missing the expected key
    '["yes"]',  # valid JSON, wrong shape
    '{"intention": null}',  # valid JSON, wrong value type
])
async def test_non_json_reply_falls_back_to_not_intended(content, caplog):
    with caplog.at_level(logging.WARNING):
        result, events = await _run(content)
    # Must not raise, and must default to "virtual screening not intended".
    assert result is False
    assert events and "no" in events[0]["check_virtual_screening_intended"].lower()
    assert any("virtual screening" in r.getMessage().lower() for r in caplog.records)
