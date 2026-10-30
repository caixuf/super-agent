"""全局配置：把「业务判断」从「引擎实现」里显式拆出来。

这个包的每一行原本都长在某个具体业务仓库里。抽出来的关键不是搬文件，
而是把**哪些是通用机制、哪些是这家公司的业务约定**分清楚：

    通用机制（本包内固定实现）
        SQLite WAL 存储、FTS + 向量双路召回、标签图、置信度衰减、
        作用域过滤、冲突判定、查重门禁、突触强化

    业务约定（由宿主通过 MemoryConfig 注入）
        领域取值（有没有某个具体业务领域名）、哪些领域带代际、
        代际怎么从文本里识别（"R1"/"R2" 还是别的写法）、默认 user/agent 名

历史教训（写在代码里是因为它是设计约束，不是故事）：
    早期把领域取值写成模块级常量元组，每加一个领域都要改代码发版 ——
    一个「能自动学习」的系统，却学不出一个新的分类。现在领域取值存在
    SQLite 注册表里，代码只负责读写，不负责枚举。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, Tuple


def _default_data_dir() -> Path:
    env = os.getenv("AGENTCORE_DATA_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".agentcore" / "data"


@dataclass
class MemoryConfig:
    """记忆引擎的全部业务可调项。

    默认值刻意保持**中性**：不出现任何具体企业的领域名或代际名，
    这样 `MemoryConfig()` 拿到的就是一个开箱可用的通用记忆引擎。
    宿主（如自动驾驶测试平台）通过参数或环境变量注入自己的约定。
    """

    # ---- 存储位置 ----
    data_dir: Path = field(default_factory=_default_data_dir)

    #: 突触账本的完整路径。None = `data_dir/memory_synapses.json`。
    #: 之所以可配：宿主的历史账本可能落在别处（例如仓库 runtime/ 下）。
    #: 路径不一致不会报错，只会让已积累的置信度/降级状态**静默清零** ——
    #: 这类"看起来正常运行、其实全部退化"的故障最难发现。
    synapse_path: Optional[Path] = None

    #: 数据目录的**动态**来源。设置后优先于 `data_dir`，且每次读写都重新求值。
    #: 为什么需要它：宿主常以环境变量决定库路径（如 MEMORY_PERSIST_DIR），
    #: 而测试会运行中改这个变量做隔离。若在 import/configure 时就把路径固化，
    #: 改环境变量将不再生效 —— 测试以为写进了 tmp，实际写进了生产库。
    data_dir_fn: Optional[Callable[[], Path]] = None

    # ---- 身份默认值 ----
    #: 未显式指定 user_id / agent_id 时的落库值
    default_user: str = "default_user"
    default_agent: str = "default_agent"

    # ---- 日志名前缀 ----
    #: 所有子模块的 logger 名 = f"{logger_prefix}.{module}"
    logger_prefix: str = "agentcore.memory"

    # ---- 分类维度种子（仅首次初始化库时使用，之后以库内为准）----
    seed_domains: Sequence[str] = (
        "general", "system", "middleware", "ai", "ops", "people",
    )
    seed_genres: Sequence[str] = ("experience", "reference", "preference", "procedure")

    # ---- 领域推断词表（业务约定，由宿主注入）----
    #: (正则, 权重, 领域)。引擎只给通用领域的默认表；宿主把自己的业务词表
    #: 通过这个字段注入，引擎里不留任何具体业务的领域名或代际名。
    seed_domain_signals: Sequence[Tuple[str, int, str]] = (
        # ---- ops：运维与流程 ----
        (r"systemctl|journalctl|service\.sh|hermes-gateway", 4, "ops"),
        (r"工作流平台|工单系统|机器人网关", 3, "ops"),
        (r"部署|发布流程|巡检|流水线|docker compose|容器编排", 3, "ops"),
        (r"哨兵|sentinel|watchdog|守护进程|重启服务", 3, "ops"),
        # ---- system：操作系统内核 ----
        (r"内核|kernel|中断描述符|系统调用|页表|进程调度", 4, "system"),
        (r"锁|自旋锁|RCU|内存屏障|cache line|TLB|NUMA", 3, "system"),
        (r"文件系统|ext4|VFS|inode|块设备驱动", 3, "system"),
        (r"cgroup|namespaces|OOM|swap|页回收", 3, "system"),
        # ---- middleware：通信中间件 ----
        (r"Fast-?DDS|RTPS|DDS 域|QoS|shared memory transport", 4, "middleware"),
        (r"ROS ?2?\b|rmw_|rclcpp|rclpy|colcon", 3, "middleware"),
        (r"组播|多播|socket|UDP|TCP|序列化|IDL", 2, "middleware"),
        # ---- autodrive：自动驾驶算法 ----
        (r"感知|感知融合|点云|激光雷达|LiDAR|摄像头标定", 4, "autodrive"),
        (r"路径规划|轨迹规划|运动规划|path planning|behavior planning", 4, "autodrive"),
        (r"SLAM|定位|建图|回环检测|IMU 预积分", 4, "autodrive"),
        (r"控制|MPC|PID|横纵向控制|阿克曼", 3, "autodrive"),
        (r"仿真|场景生成|Sim-to-Real|数字孪生|world model", 3, "autodrive"),
        (r"Autoware|Apollo|Waymo|Robotaxi|端到端驾驶", 3, "autodrive"),
        # ---- ai：大模型与 agent ----
        (r"大模型|LLM|prompt|提示词|token|embedding|向量化", 4, "ai"),
        (r"agent|智能体|记忆系统|RAG|微调|推理链", 3, "ai"),
        # ---- automotive：通用车辆通识（无代际）----
        (r"AUTOSAR|CAN 总线|功能安全|ISO 26262|ASIL|SOME/IP", 4, "automotive"),
        (r"EtherCAT|车载以太网|线控底盘", 3, "automotive"),
    )

    #: 来源文件前缀 -> 领域。书籍/文档成体系，同一文件的块领域理应一致。
    seed_source_prefixes: Sequence[Tuple[str, str]] = (
        ("linux-", "system"),
        ("world_models", "autodrive"),
        ("compute_chips", "autodrive"),
        ("path_planning", "autodrive"),
        ("planning", "autodrive"),
        ("perception", "autodrive"),
        ("sensors", "autodrive"),
        ("localization", "autodrive"),
        ("control", "autodrive"),
        ("slam", "autodrive"),
        ("fusion", "autodrive"),
        ("end_to_end", "autodrive"),
        ("robotaxi", "autodrive"),
        ("open_source_platforms", "autodrive"),
        ("foundation", "autodrive"),
        ("methodology", "autodrive"),
        ("autoware", "autodrive"),
        ("apollo", "autodrive"),
        ("waymo", "autodrive"),
        ("tesla", "autodrive"),
        ("vlm", "ai"),
        ("dds", "middleware"),
        ("rtps", "middleware"),
        ("ros2", "middleware"),
        ("middleware", "middleware"),
        ("feishu", "ops"),
        ("hermes", "ops"),
        ("gateway", "ops"),
        ("sentinel", "ops"),
        ("deploy", "ops"),
        ("secoc", "automotive"),
        ("hsm", "automotive"),
        ("autosar", "automotive"),
        ("iso26262", "automotive"),
        ("functional_safety", "automotive"),
        ("apachecn-linux", "system"),
        ("apachecn-cs", "system"),
        ("apachecn-python", "ai"),
        ("fucking-algorithm", "ai"),
        ("miaosha", "middleware"),
        ("algorithm", "ai"),
        ("leetcode", "ai"),
        ("data_structure", "ai"),
    )

    # ---- 代际（era / generation）----
    #: 哪些领域带代际概念。空 = 这个部署里没有代际维度（纯通用场景）。
    era_domains: Sequence[str] = ()

    #: 从文本里识别代际的正则。默认匹配 "G1" / "G2.1" 这类写法
    #: （宿主按自己的代际命名注入覆盖），捕获组 1 是代际主体（用于拼桶名）。
    era_pattern: str = r"\bG(\d+)(?:\.\d+)?\b"

    #: 桶名前缀：识别出编号之后拼成什么（宿主注入自己的前缀）
    era_prefix: str = "G"

    #: 无代际信息时的桶名
    unknown_bucket: str = "_unknown"

    # ---- 嵌入 ----
    #: 向量维度。必须与 embed 模型输出一致，否则 vec0 表建错维度、KNN 全空。
    embed_dim: int = 1024

    # ---- 可注入的外部能力 ----
    #: 文本嵌入函数 (list[str]) -> list[list[float]] | None
    #: None = 由内置实现按环境变量决定（OpenAI 兼容端点 / Ollama）
    embed_fn: Optional[Callable[[Sequence[str]], Optional[list]]] = None

    #: 兜底向量化后端链。内置 ollama 不可用时按顺序尝试，第一个成功即采用。
    #: 宿主把外网/自建嵌入服务的实现放这里，引擎因此不必 import 任何嵌入 SDK。
    embed_fallbacks: Sequence[Callable[[Sequence[str]], Optional[list]]] = ()

    #: Explicit host attestation that fallback backends produce vectors in
    #: exactly the same model space as the primary. Dimensions alone do not
    #: establish compatibility. False degrades to non-vector retrieval.
    embed_fallbacks_same_model: bool = False

    #: 对话补全函数，签名需兼容 chat_completions(messages, timeout=, max_tokens=, temperature=)
    #: 用于事实抽取与冲突裁决这两处需要语义判断的环节。
    llm_fn: Optional[Callable[..., Any]] = None

    #: 对话补全的**懒加载**器。宿主常有"先补 sys.path 再导入"的加载逻辑，
    #: 且不想在 import 期就付出代价；这里让宿主把那段逻辑交进来。
    #: 解析顺序：llm_fn > llm_loader() > None（None = 该环节降级跳过）。
    llm_loader: Optional[Callable[[], Optional[Callable[..., Any]]]] = None

    #: 突触账本的写入器。宿主已有账本实现（含其路径与历史数据）时注入它，
    #: 保证**只有一个 owner**：两套实现各写各的文件，会表现为「强化了但读不到」。
    reinforcer: Optional[Callable[..., Any]] = None

    #: 突触账本的读取器，配合 reinforcer 使用（返回 {id: {confidence, status, ...}}）。
    synapse_loader: Optional[Callable[[], Optional[dict]]] = None

    # ---- 批量灌入 ----
    #: 灌书/灌文档时放宽 fsync（可重跑的批处理，丢了重灌即可）
    bulk_ingest: bool = field(
        default_factory=lambda: os.getenv("MEMORY_BULK_INGEST", "").strip() == "1")

    def base_dir(self) -> Path:
        """当前数据目录。data_dir_fn 优先，每次调用重新求值（环境变量可运行时变）。"""
        if self.data_dir_fn is not None:
            return Path(self.data_dir_fn())
        return Path(self.data_dir)

    def db_path(self) -> Path:
        return self.base_dir() / "coding_memory_cv.db"

    def registry_db_path(self) -> Path:
        return self.base_dir() / "memory_registry.db"

    def trace_db_path(self) -> Path:
        return self.base_dir() / "memory_usage.db"

    def synapse_store_path(self) -> Path:
        return self.synapse_path or (self.base_dir() / "memory_synapses.json")

    # ------------------------------------------------------------------
    def resolve_llm(self) -> Optional[Callable[..., Any]]:
        """取当前可用的对话补全函数。没有就返回 None（调用方降级跳过）。

        引擎**不自己导入任何 LLM 客户端** —— 那是宿主的依赖。
        内置实现一旦写死某个包名，这个库就不再通用。
        """
        if self.llm_fn is not None:
            return self.llm_fn
        if self.llm_loader is not None:
            try:
                return self.llm_loader()
            except Exception:  # noqa: BLE001 —— 加载失败=没有，由调用方降级
                import logging
                logging.getLogger(f"{self.logger_prefix}.config").warning(
                    "llm_loader 调用失败，本环节降级", exc_info=True)
                return None
        return None

    # ------------------------------------------------------------------
    def normalize_era(self, value: Optional[str]) -> str:
        """把任意写法归一成桶名。认不出就进 unknown_bucket。

        绝不默认当成「都适用」—— 一条判不出代际的经验如果对所有代际生效，
        就等于用一个未知范围的规则覆盖了已知范围。
        """
        v = str(value or "").strip().upper()
        if not v:
            return self.unknown_bucket
        hits = set(re.findall(self.era_pattern, v, re.IGNORECASE))
        if len(hits) == 1:
            return f"{self.era_prefix}{hits.pop()}"
        return self.unknown_bucket

    def has_era(self, domain: Optional[str]) -> bool:
        return str(domain or "").strip().lower() in {
            str(d).strip().lower() for d in self.era_domains}


#: 进程级当前配置。`configure()` 替换它；所有子模块通过 `get_config()` 读，
#: 这样宿主可以在导入后统一注入，不必给每个函数加参数。
_ACTIVE: MemoryConfig = MemoryConfig()


def get_config() -> MemoryConfig:
    return _ACTIVE


def configure(cfg: MemoryConfig) -> MemoryConfig:
    """安装全局配置。返回被安装的配置，便于链式使用。"""
    global _ACTIVE
    _ACTIVE = cfg
    return _ACTIVE


def reset_config() -> MemoryConfig:
    """恢复中性默认配置（测试用）。"""
    global _ACTIVE
    _ACTIVE = MemoryConfig()
    return _ACTIVE


def logger_name(module: str) -> str:
    return f"{get_config().logger_prefix}.{module}"
