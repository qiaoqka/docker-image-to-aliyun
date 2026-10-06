# 项目介绍

利用GitHub Actions自动化构建并推送Docker Image到阿里云容器镜像服务。

## 使用方式

### 配置阿里云

登录阿里云容器镜像服务

https://cr.console.aliyun.com/

启用个人实例，创建一个命名空间（**ALIYUN_NAME_SPACE**）

![命名空间](doc/命名空间.png)

访问凭证–>获取环境变量  
用户名（**ALIYUN_REGISTRY_USER**）  
密码（**ALIYUN_REGISTRY_PASSWORD**）  
仓库地址（**ALIYUN_REGISTRY**）  

![用户名密码](doc/用户名密码.png)

密码在刚创建个人实例时会提示创建，请自行保管

### Fork本项目

Fork本项目  
进入您自己的项目，点击Action，启用Github Action功能
配置环境变量，进入Settings->Secret and variables->Actions->New Repository secret
![配置环境变量](doc/配置环境变量.png)
将上一步的 ALIYUN_NAME_SPACE，ALIYUN_REGISTRY_USER，ALIYUN_REGISTRY_PASSWORD，ALIYUN_REGISTRY
的值配置成环境变量

### 添加镜像

镜像清单是仓库根目录的 `images.yaml`。每条至少写 `source`，可选 `platform` 与 `local`：

```yaml
images:
  - source: alpine
  - source: oven/bun:1.2.19-alpine
  - source: continuumio/miniconda3:24.9.2-0
    platform: [linux/arm64]
  - source: xiaoyaliu/alist
    platform: [linux/arm64, linux/arm/v7]
  - source: alpine/minio:RELEASE.2025-10-15T17-29-55Z
    local: minio/minio:latest
```

- `source`（必填）：上游镜像，可以带 tag；不写 tag 按 `latest` 处理。
- `platform`（可选）：架构字符串列表，例如 `[linux/arm64]`、`[linux/arm64, linux/arm/v7]`。省略时按 `linux/amd64` 处理，阿里云 tag 不带架构后缀；显式指定会在阿里云 tag 后加 `-linux-arm64` 这类后缀。列表按顺序逐项展开成多条，单架构也要写成单元素列表；同一镜像多架构合并到一个列表即可，不要重复写多条同 source 条目。
- `local`（可选）：镜像拉到目标机后额外打的本地 tag。Action 不使用该字段，`运维脚本/ssh-docker-pull` 拉取脚本使用。多个架构展开时该值应用到每一条。

修改 `images.yaml` 并 push 到 `main` 即触发同步；也可以在 Actions 页面手动 Run workflow。

自动触发只监听 `images.yaml`、`.github/workflows/docker.yml`、`scripts/**` 的变更。同步串行处理全清单：逐条比对源与阿里云的 `config.digest`，相同就跳过，不同或无法确定才拉取并推送；任意一条失败都会让本次运行变红。

digest 检查依赖 Docker Buildx：直接读取原始 manifest；遇到 index 时只查询匹配架构的一个子 manifest，不展开其他架构或读取 config blob。插件缺失会在登录前失败，避免把运行前提错误当成全清单需要更新。GitHub runner 固定为 `ubuntu-24.04`；官方 checkout、Buildx 与诊断结果上传 Action 均使用完整 commit SHA。

### 上传对照诊断

独立入口是 [push-perf.yml](/.github/workflows/push-perf.yml)，实现是 [push-perf.py](/diagnostics/push-perf.py)。只有无 inputs 的手动入口；推送诊断文件不会自动上传测试内容。正常同步与诊断 job 共用 `aliyun-image-upload` 并发锁，不取消正在上传的任务。该锁不覆盖本机或其他仓库，对照期间不要同时从控制端上传。

前提：在既有命名空间下创建*私有* `image-sync-perf` 仓库。诊断只使用该仓，不借用业务镜像仓；认证或仓库检查失败会明确退出。GitHub 使用现有四项 `ALIYUN_*` secrets，不增加凭据 inputs。

通过 CLI 启动 GitHub 对照：

```sh
gh workflow run push-perf.yml
```

控制端先通过既有凭据管理方式向进程提供 `ALIYUN_REGISTRY`、`ALIYUN_NAME_SPACE`、`ALIYUN_REGISTRY_USER`、`ALIYUN_REGISTRY_PASSWORD`，不要将密码放进 argv 或公开文件。仅运行 HTTP 臂，不要求本机 Docker daemon：

```sh
mkdir -p /tmp/opencode-workspaces/docker
RESULT_DIR="$(mktemp -d /tmp/opencode-workspaces/docker/perf.XXXXXX)"
python3 diagnostics/push-perf.py --http-only --environment control \
  --auth-origin https://dockerauth.cn-hangzhou.aliyuncs.com \
  --output "$RESULT_DIR/perf-summary.json" \
  --summary "$RESULT_DIR/perf-summary.md"
```

`--auth-origin` 只显式信任一个完整 HTTPS 认证 origin，默认仅信任 Registry 自身。阿里云认证端点是经官方文档与真实 challenge 核验的 [`dockerauth.cn-hangzhou.aliyuncs.com`](https://help.aliyun.com/en/acr/user-guide/use-cr-diagnosis-to-troubleshoot-image-push-and-pull-exceptions)；不同主机、端口、HTTP 降级和任意重定向不受信任。认证信任不会自动扩大 upload Location 的权限。

每端每次生成全新 64 MiB 随机内容，用一次持续 PATCH 测量真实 body 传输，再独立测提交和 HEAD 验证。原始 HTTP 臂止于 blob，不发布 manifest 或 tag；GitHub 另以同一载荷生成单层镜像，使用独立测试 tag 执行真实 Docker 推送，并报告 gzip 参考值。已有 blob、Docker 去重、descriptor 不一致或缺少真实上传证据的样本不能作为速度结论。连接上限 30 秒；原始 HTTP 与 Docker 推送各自最多 35 分钟；CI job 最多 90 分钟。不自动重试大 body 或替换失败样本。

JSON、GitHub step summary 和 artifact 只保存去敏状态、阶段耗时、字节数、测试 tag/digest、版本及清理结果，不保存密码、token、签名 Location 或原始异常。结束时清理本地对象，只取消本次未完成 upload，并尝试删除 Docker 臂本次 manifest/tag。已提交的原始 blob、响应丢失后的未知提交、删除拒绝及孤立 blob 会报告精确残留；不执行全仓删除或 blob GC，不宣称存储已释放。首次每端一个样本是探索性证据，不能据此判定稳定链路差异或确定根因。

### 使用镜像

个人实例的镜像仓库默认私有，pull 前必须 `docker login` 登录，不建议把仓库改成公开。

在国内服务器pull镜像：

``` sh
docker login crpi-xx.cn-shanghai.personal.cr.aliyuncs.com
docker pull crpi-xx.cn-shanghai.personal.cr.aliyuncs.com/{自己的命名空间}/alpine
```

> 其中crpi-xx.cn-shanghai.personal.cr.aliyuncs.com 即 ALIYUN_REGISTRY  
> alpine 即 images.yaml 里面填的镜像
