---
name: im-card-markdown
description: Use when 要把 Markdown 回复渲染成 IM 卡片（表格、列表、标题、代码块），或卡片出现「源码泄露 / 表格重复 / 请升级客户端 / 整卡退化成纯文本 / 长消息只渲染一半」时。覆盖卡片结构版本差异（1.0 lark_md、2.0 markdown+table、card_components v2）、每帧共享卡要求与可复现的判据。
version: 1.0.0
license: MIT
metadata:
  hermes:
    tags: [feishu, card, markdown, rendering, chat-ui]
    related_skills: [research-before-build]
---

# IM 卡片渲染 Markdown：能渲染什么、在哪一版

## 技能定位

回答「这段 Markdown 放进卡片，会不会显示成源码」以及「卡片报错/半截渲染时查哪一层」。
**不适用于**：纯文本消息（不需要卡片）、图文文档（那走渲染器出 HTML/PNG）。

> 核心判据：**先确定卡片的结构版本，再决定用哪个文本组件；版本不对，内容再对也白搭。**

## 一、三套结构，各自能渲染什么

| 结构 | 文本组件 | 表格 | 列表 | 标题 | 代码块 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 1.0（无 `schema`） | `div` + `text.lark_md` | ✗（显示竖线） | ✗（显示 `- x`） | ✗ | ✗ |
| 2.0（`schema: "2.0"`） | `markdown` 元素 | ✓ 原生 table 或 markdown 表格 | ✓ | ✓ h1–h3 | ✓ |
| card_components v2（客户端「2.4」） | `div` + `text.plain_text` | ✓ table 组件 | ✗ | ✗ | ✗ |

三条硬事实（实测）：

- 2.0 卡发到 v2 客户端会**整卡**降级成「请升级客户端」——所以要么全 2.0，要么别用 2.0；
- 纯 `plain_text` **不能**做独立元素，正文必须写成 `div` + `text.plain_text`，否则 `200621 type of element is not supported tag: plain_text`；
- 表格 `row_height: "auto"` 会被拒（`200905`）；用 `low/middle/high` 或像素值。

## 二、正文要“先过一遍能不能渲染”

```
如果含表格 / 列表 / 标题 / 代码块  → 必须走 2.0 的 markdown 元素
否则                              → 1.0 的 lark_md 更稳（宽度、字号按 1.0 调过）
```

判定要**按行判定**（表格 = 表头行 + 分隔行），不要只看有没有竖线字符。同一张卡里**不要放纯文本副本**：
markdown 元素与 plain_text 副本并存时，表格会被画两遍。

## 三、流式与更新

- 一条消息的卡片**结构版本不可变**：先发 1.0 就别想 PATCH 成 2.0，只能新发一条并把旧卡改成一句话指路；
- 一旦发的是 2.0 卡，后续阶段（图文、附件说明）**原地 PATCH**，不要再发新卡；
- PATCH 要求更新前后都是共享卡：`config.update_multi: true` 每一帧都要带。

## 四、报错速查

| 错误码 / 现象 | 含义 | 修法 |
| :--- | :--- | :--- |
| `200861 unsupported tag note` | 1.0 标签（note/markdown）出现在 v2 卡里 | 去掉或改 `div`+`plain_text` |
| `200621 ... tag: plain_text` | 裸 `plain_text` 元素 | 包成 `div` |
| `200905 table row height is invalid` | `row_height: "auto"` | 改 `low` |
| 表格画了、后面正文变成源码 | 单个 markdown 块过长，客户端截断 | 拆成多个 markdown 块或缩短 |
| 整卡「请升级客户端」 | 2.0 卡发给 v2 客户端 | 改 1.0/div+plain_text，或升级客户端 |

## 五、验收

- 发一条含 14 行表格 + 表格后说明的真实回复，肉眼确认：原生表格、**只出现一次**、说明是正文；
- 中转/兜底的纯文本链路不能出现竖线与 `#` 标题标记；
- 回归：把这条回复喂给卡片构造器，断言元素标签序列（mock 即可）。
