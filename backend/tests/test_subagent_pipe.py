from __future__ import annotations

from app.tools.builtin.pipe import MAX_PIPE_TOTAL_CHARS, MAX_PIPE_WRITES, SubagentPipe


def test_subagent_pipe_writes_parts_and_finishes() -> None:
    pipe = SubagentPipe()

    first = pipe.write({"content": "第一段"})
    second = pipe.write({"content": "第二段"})
    finished = pipe.finish({})

    assert first["status"] == "success"
    assert first["output"]["part_index"] == 1
    assert first["output"]["written_chars"] == 3
    assert first["output"]["total_chars"] == 3
    assert first["output"]["remaining_writes"] == MAX_PIPE_WRITES - 1
    assert first["output"]["remaining_chars"] == MAX_PIPE_TOTAL_CHARS - 3
    assert first["output"]["max_writes"] == MAX_PIPE_WRITES
    assert first["output"]["max_total_chars"] == MAX_PIPE_TOTAL_CHARS
    assert first["output"]["should_finish"] is False
    assert first["output"]["auto_finished"] is False
    assert second["output"]["part_index"] == 2
    assert pipe.content() == "第一段\n第二段"
    assert finished["status"] == "success"
    assert finished["output"]["status"] == "done"
    assert finished["output"]["parts"] == 2
    assert finished["output"]["total_chars"] == 6
    assert finished["output"]["remaining_writes"] == MAX_PIPE_WRITES - 2
    assert finished["output"]["remaining_chars"] == MAX_PIPE_TOTAL_CHARS - 6


def test_subagent_pipe_rejects_non_string_content_and_finish_arguments() -> None:
    pipe = SubagentPipe()

    invalid_write = pipe.write({"content": {"text": "不允许"}})
    invalid_finish = pipe.finish({"summary": "不允许"})

    assert invalid_write["status"] == "failed"
    assert invalid_write["output"]["code"] == "invalid_pipe_content"
    assert invalid_finish["status"] == "failed"
    assert invalid_finish["output"]["code"] == "invalid_finish_arguments"


def test_subagent_pipe_rejects_total_budget_exceeded_without_truncation() -> None:
    pipe = SubagentPipe(max_total_chars=5)

    first = pipe.write({"content": "1234"})
    exceeded = pipe.write({"content": "56"})

    assert first["status"] == "success"
    assert first["output"]["remaining_chars"] == 1
    assert exceeded["status"] == "failed"
    assert exceeded["output"]["code"] == "pipe_budget_exceeded"
    assert exceeded["output"]["remaining_chars"] == 1
    assert exceeded["output"]["attempted_chars"] == 2
    assert pipe.content() == "1234"


def test_subagent_pipe_auto_finishes_when_write_count_budget_is_exhausted() -> None:
    pipe = SubagentPipe(max_writes=2, max_total_chars=100)

    first = pipe.write({"content": "第一段"})
    second = pipe.write({"content": "第二段"})

    assert first["output"]["auto_finished"] is False
    assert second["status"] == "success"
    assert second["output"]["remaining_writes"] == 0
    assert second["output"]["should_finish"] is True
    assert second["output"]["auto_finished"] is True
    assert second["output"]["reason"] == "pipe_budget_exhausted"
    assert pipe.auto_finished is True


def test_subagent_pipe_auto_finishes_when_total_char_budget_is_exhausted() -> None:
    pipe = SubagentPipe(max_writes=10, max_total_chars=6)

    result = pipe.write({"content": "123456"})

    assert result["status"] == "success"
    assert result["output"]["remaining_chars"] == 0
    assert result["output"]["should_finish"] is True
    assert result["output"]["auto_finished"] is True
    assert pipe.content() == "123456"
