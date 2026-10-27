# 灌灌中转链路的表格适配：可直接套用的补丁

日期：2026-10-27。目标：**经灌灌转发的回复**也能显示成飞书原生表格（现在经灌灌看到的是 `|` 源码）。

## 现场事实（经 emily 跳到灌灌实测）

- 灌灌桥：`~/Downloads/feishu-cc-py/bridge/relay_jobs.py`，`_send_card()`（约 264–285 行）把结果包成
  **卡片 JSON 1.0** 的单个 `div/lark_md`：
  ```python
  {"config": {"wide_screen_mode": True},
   "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": text}}]}
  ```
  卡片 1.0 的 `lark_md` **不渲染表格** —— 这是 `|` 源码的直接原因。
- `text` 取自引擎返回的 `content`（约 426 行 `str(result.get("content") or "文件已生成。")`）。
- 飞书侧的限制（emily/本机都实测过）：走 card_components v2 时，卡片的 `table` 组件会被客户端
  降级成「请升级客户端」占位，**表格只能出现在 2.0（1.0 结构 + `schema` 缺省）的卡里**。

## 结论（诚实版）

「同一张卡里既有 `lark_md` 正文、又有原生表格」在 2.4 客户端上做不到 —— 除非改用 2.0 卡片，
但 2.0 卡片在 2.4 客户端上会**整卡**降级成升级提示。所以灌灌这条链路能达标的最稳做法是：

> **带表格的回复不走同卡**：发一条 2.0 卡片（含原生表格），正文文字也放 2.0 的 `markdown` 组件。

用户看到的是表格正常、正文正常，代价是这条回复不再是「1.0 那张浅色卡」的样式。

## 补丁（`bridge/relay_jobs.py`）

在文件里加一个判定 + 一个构造器，`_send_card` 与 PATCH 两处都用它。

```python
_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")
_DIVIDER = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+")

def _card_20_or_10(text: str) -> dict:
    """2.0 card when the reply carries a table (native table), else the plain 1.0 card."""
    lines = text.splitlines()
    has_table = any(_TABLE_LINE.match(line) and _DIVIDER.match(lines[i + 1])
                    for i, line in enumerate(lines[:-1]))
    if not has_table:
        return {"config": {"wide_screen_mode": True},
                "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": text}}]}
    return {"schema": "2.0", "config": {"update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": "IMFL 高手"}, "template": "blue"},
            "body": {"elements": [{"tag": "markdown", "content": text}]}}
```

- `_send_card()` 的创建与 PATCH 两处把原来的 dict 换成 `_card_20_or_10(text)`；
- PATCH 对已存在的 1.0 卡**不能**换成 2.0（同一 message 的结构版本不可变），所以带表格的回复在
  `_send_card` 里应先判断：已有 `card_id` 且本次需要 2.0 → 另发一条新卡并结束旧卡，而不是 PATCH。

## 验收

1. 通过灌灌问一句会出表的问题（例：把最近几次跑测按需求列个表）；
2. 期望：飞书里出现一张卡，表格是**原生表格**（不是 `|` 源码、不是「请升级客户端」）；
3. 同时发一句纯文字问题：仍应是原来的浅色卡。

## 未做 / 边界

- 本仓没有灌灌端的代码（在另一台机器上），所以这里只给补丁；改之前请备份 `relay_jobs.py`；
- 若灌灌桥对同一条消息有「一任务一卡」的硬约束，需要按上面第 2 条改成「新卡替换旧卡」；
- `display_markdown`（Emily 引擎已返回，表格已对齐成文本）可作兜底：不支持原生表格的路径至少不会看到 `|`。
