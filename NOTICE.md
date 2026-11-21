# 致谢与来源说明

本项目（CDT-Management）是独立实现。

## 安装脚本

[**BeacherZ/dailycheckin-plus**](https://github.com/BeacherZ/dailycheckin-plus) — MIT。

参考的部分：

- 一键脚本的整体骨架：root 校验 → Docker 自动安装 → 镜像版本比对 → 版本一致则不动容器
- 交互式升级/卸载菜单，以及 `UNINSTALL=1` / `UNINSTALL=docker` 的分级卸载
- `PORT` / `DIR` / `FORCE` / `NO_DOCKER_INSTALL` 这组覆盖变量的命名与语义
- 装完打印内网/公网访问地址的做法

## 第三方依赖

| 依赖 | 用途 | 许可 |
| --- | --- | --- |
| [FastAPI](https://github.com/fastapi/fastapi) | HTTP 服务 | MIT |
| [uvicorn](https://github.com/encode/uvicorn) | ASGI 服务器 | BSD-3-Clause |
| [cryptography](https://github.com/pyca/cryptography) | AES-GCM 字段加密 | Apache-2.0 / BSD-3-Clause |
| [requests](https://github.com/psf/requests) | HTTP 客户端 | Apache-2.0 |
| [aliyun-python-sdk-core](https://github.com/aliyun/aliyun-openapi-python-sdk) | 阿里云 RPC 签名 | Apache-2.0 |
| [aliyun-python-sdk-ecs](https://github.com/aliyun/aliyun-openapi-python-sdk) | ECS 启停与查询 | Apache-2.0 |
| [React](https://github.com/facebook/react) / [Vite](https://github.com/vitejs/vite) / [lucide-react](https://github.com/lucide-icons/lucide) | 前端 | MIT / ISC |

## 服务接口

- 阿里云 CDT：`ListCdtInternetTraffic`（2021-08-13）
- 阿里云 ECS：`DescribeInstances` / `StartInstances` / `StopInstances`（2014-05-26）
- Cloudflare：`/zones`、`/zones/{id}/dns_records`

## 关于「换班」这套逻辑

AM/PM 双账号轮换、流量阈值保护、故障转移、全局熔断、超限停机、抢占恢复、换班超时告警、每日汇总 —— 这些行为约定来自项目作者自用的保活脚本，本项目把它重构成了**支持 N 台、可自定义各自的运行时长**的队列式轮换，并把所有执行痕迹落 SQLite，保证容器重启后不重复推送、不重复开关机。
