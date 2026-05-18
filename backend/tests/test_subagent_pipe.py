from __future__ import annotations

from app.runtime.executor.subagent_pipe import SubagentPipe


def test_subagent_pipe_writes_parts_and_finishes() -> None:
    pipe = SubagentPipe()

    first = pipe.write({"content": "第一段"})
    second = pipe.write({"content": "第二段"})
    finished = pipe.finish({})

    assert first["status"] == "success"
    assert first["output"]["part_index"] == 1
    assert first["output"]["written_chars"] == 3
    assert second["output"]["part_index"] == 2
    assert pipe.content() == "第一段\n第二段"
    assert finished == {
        "status": "success",
        "output": {"status": "done", "parts": 2, "total_chars": 6},
    }


def test_subagent_pipe_rejects_non_string_content_and_finish_arguments() -> None:
    pipe = SubagentPipe()

    invalid_write = pipe.write({"content": {"text": "不允许"}})
    invalid_finish = pipe.finish({"summary": "不允许"})

    assert invalid_write["status"] == "failed"
    assert invalid_write["output"]["code"] == "invalid_pipe_content"
    assert invalid_finish["status"] == "failed"
    assert invalid_finish["output"]["code"] == "invalid_finish_arguments"
