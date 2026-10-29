"""agentcore —— 通用记忆引擎。

定位：一个**与业务无关**的长期记忆引擎。存储、召回、分桶、冲突判定、
可塑性这些机制都在这里固定实现；「哪个领域、哪一代、默认是谁、用哪个
嵌入模型」这些约定由宿主通过 :class:`agentcore.memory.config.MemoryConfig`
注入。

本包不出现任何具体企业的领域名、代际名或专有名词，这条约束由测试
``tests/test_business_neutrality.py`` 强制执行。
"""

from __future__ import annotations

from . import memory
from .memory import (
    MemoryConfig,
    configure,
    flush_background,
    get_config,
    recall_facts,
    reset_config,
    write_fact,
)

__version__ = "0.1.0"

__all__ = [
    "MemoryConfig",
    "configure",
    "get_config",
    "reset_config",
    "write_fact",
    "recall_facts",
    "flush_background",
    "memory",
    "__version__",
]