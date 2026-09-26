# 联动耕地保护与宅基地退出基础平台

本项目是一套可离线运行的 Python 服务端平台，供县、乡镇和村级工作人员管理新型城镇化安置、土地资源分配、危房安全勘察与改造复核。账号登录、角色权限、业务状态、幂等结果和审计事件保存在 SQLite 中，适合安置经办、自然资源、住建复核与审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/rural_allocation/`：乡镇片区、地块资源池、土地批次、家庭申请、分配运行与移交情景；
- `src/rural_allocation/linkage.py`：耕地保护与宅基地退出联动，登记地块版本、保护范围、家庭主体授权与承诺到期时间，受理时同步校验，按确定版本生成安置与土地方案；
- `src/housing_safety/`：危房勘察协议、测量导入、异常复核、分析任务租约和安全结论；
- `src/remediation_review/`：改造案件、现场测量、风险分析、账号登录与质量审批；
- `fixtures/`：离线验收使用的勘察协议和结构化测点；
- `tests/`：核心规则、权限、错误边界、事务、API 和命令行验收测试。

## 耕地保护与宅基地退出联动

- 县级自然资源角色登记地块版本并核定确定版本，批量导入永久基本农田与用途管制保护规则；导入全有或全无，重复提交按幂等键返回稳定结果。
- 经办人登记家庭主体授权、成员资格与复垦承诺到期时间；受理宅基地退出时同步校验补充承包地不占永久基本农田、家庭成员具备资格、复垦承诺与授权未到期，校验不通过留痕审计。
- 候选地块只在同时满足用途管制、家庭权益和基础设施边界的区域生成，每个被排除地块都给出具体依据；安置与土地方案必须引用确定版本，后续新版本不会改写既有方案。
- 资格撤回立即阻止尚未确认的方案，已确认未开工与施工中的方案进入人工处置，已经发生的合法交付保留，地块引用不做静默迁移；方案确认与开工时重新校验家庭权益。
- 家庭账号只能查看本家庭的最小必要视图，经办、自然资源与审计角色查看完整业务视图，候选排除依据对所有相关角色公开。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m rural_allocation.acceptance --workspace .
PYTHONPATH=src python3 -m housing_safety.acceptance --workspace .
PYTHONPATH=src python3 -m remediation_review.acceptance
```

三条命令使用临时 SQLite 数据库完成村镇与地块登记、家庭申请分配、危房测量分析和改造审批，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m rural_allocation.api --database rural.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m housing_safety.api --database housing.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m remediation_review.api --database remediation.sqlite3 --host 127.0.0.1 --port 8082
```

服务均提供 `GET /health`，其余接口使用 JSON。账号登录和角色权限由服务端校验，进程重启后可以继续查询 SQLite 中的业务状态与审计历史。
