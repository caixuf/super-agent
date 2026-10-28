
## 在活的 Hermes 上启用（重要）

宿主 cron 的合并路径对「直接写进 `jobs.json` 的作业」**不会重算 `next_run_at`**
（`cron/job_definition.py`：只有本地记录与导入记录合并时才计算落点）。因此正确做法是：

```bash
# 1) 用官方入口逐条创建。`cron create` 按名称**不幂等**——重复创建会得到重复作业
hermes --profile <p> cron create "<cron 表达式>" "<autotask render 出来的提示词>" \
  --name <task id> --deliver platform:<chat_id> --workdir <业务工作目录>

# 2) 列表核对：每条 task id 只能出现一次
hermes --profile <p> cron list | grep -c "<task id>"

# 3) 手动跑一次，确认产物与判据都在
hermes --profile <p> cron run <job id>
```

`install_jobs.py` 只用于**离线编辑** `jobs.json`（例如打补丁前的基线或迁移草稿），
不要用它代替官方入口启用。
