from agentcore.memory.memory_store import _passes_floor


def test_short_identifier_is_not_a_substring_of_another_protocol():
    mpi = dict(subject='Parallel runtime', statement='MPI 执行模型支持并行处理。',
               tags={'scope': 'reference'}, _score=0.61)
    assert not _passes_floor(mpi, 0.52, 'Pi 执行器')
    pi = {**mpi, 'statement': 'Pi 执行器使用 JSON 事件确认工具执行结果。'}
    assert _passes_floor(pi, 0.52, 'Pi 执行器')


def test_platform_preference_keeps_its_existing_semantic_floor():
    preference = dict(statement='先完成全部任务，之后优化', tags={'scope': 'platform'}, _score=0.57)
    assert _passes_floor(preference, 0.52, '完成任务的顺序')
