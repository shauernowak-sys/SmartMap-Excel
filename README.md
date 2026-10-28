# SmartMap Excel

Excel明细账到财务凭证模板的确定性转换与验证工具，当前版本`0.2.0.dev24`。

提供工作表和表头识别、凭证与账期解析、科目层级排重、可配置科目映射、整凭证筛选、辅助核算、借贷平衡检查、月末日期、模板写出和逐行追溯。核心不调用大模型API，不上传业务数据。

## 安装

需要Python 3.10+；已在Python 3.12环境验证。

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m unittest discover -s tests -v
```

## 使用

先根据业务审核配置，再执行转换；所有输出须使用新目录。

```powershell
python -m smartmap config-check --config config/local/rules.json
python -m smartmap convert --source "本地原始明细账.xlsx" --config config/local/rules.json --out reports/conversion/new_run
```

`config/example.json`和`config/auxiliary.example.json`提供合成示例；其中候选规则不是企业已批准规则。完整配置、模板写出、独立验收和退出码说明见[中文使用说明](docs/使用说明.md)。

## 工程状态

已完成一个获授权真实案例的转换与历史成品验收，本地完整测试479项通过。仓库不包含企业样本、个人及往来档案、获批业务配置、凭证成品或敏感报告；缺少本地业务文件时，相关集成测试会明确跳过。其余合成测试可独立执行。

当前提供命令行工具，尚无完整图形界面。每个企业、月份及系统模板均应使用自己的审核配置，不能把一个案例的规则当成全局规则。

## 目录

- `smartmap/`：转换引擎与模板适配。
- `tools/`：只读分析、独立审计及验收工具。
- `tests/`：合成单元测试及可选本地集成测试。
- `config/`：配置格式和合成示例。
- `verifier/`：V0.1历史源码，供行为审计使用。

默认不覆盖输入文件，不静默删除异常记录，不自动改写借贷金额。所有被排除记录均记录原因及原始Excel行；存在异常时按整张凭证处理，部分导出必须显式选择。
