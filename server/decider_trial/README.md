# recordian-decider-trial

111 上的 Mapika/decider-4b v2 隔离试用。不改 Recordian 产品，不碰 SemIf（42032）和 Winnow（42070）。

## 钉住的版本

- 代码：https://github.com/Mapika/decider commit `5f91c011f05fa4b685f0845281a0805a56eb0169`（decider-ai 1.3.0，本地 clone 在 `/work/src/decider`）
- 权重：`Mapika/decider-4b` revision `49564ddcfccafb6db563eb757c1d41e6c78dcb56`，单个 `model.safetensors`
  8411558400 字节、426 tensors、sha256 `69e6895461c425c6469cd304838a2e5673141613c2da04782026e37c1481d936`
- `decider_config.json`：4b-v2、temperature 1.935、layout plain、schema_first false
- 基座镜像：`local/qwen-retrieval-gpustack:rocm`（`b0860b7e5025`），torch `2.10.0+rocm7.2.3`，**不替换 torch**

## 目录与容器

- 数据盘：`/media/v/Data/recordian-decider-trial`（源码、venv、权重、缓存、日志）
- 服务目录：`/home/v/services/recordian-decider-trial`（回滚时一起删）
- 容器名：`recordian-decider-trial`，端口 `192.168.5.111:42071`（只绑 111，不绑 0.0.0.0；`127.0.0.1:42071` 故意不可达）
- `--restart=no`（仍是 trial）、内存上限 18g、CPU 4、`--pids-limit 512`、runtime `runc`
- 设备：`/dev/kfd` 与 `/dev/dri`，`--group-add 992`（render）和 `--group-add 44`（video）

镜像里没有名为 render 的组，所以不能写 `--group-add render`。`venv --system-site-packages` 实际指向
`/usr/bin/python3.12`，看不到 `/opt/venv`；venv 里用 `rocm-image-venv.pth` 指向镜像的 site-packages，
本目录安装的 numpy 1.26 和 transformers 5 覆盖镜像里的旧版本，torch 仍来自镜像。

## 启动就绪：先完成真实预热，再开放接口

问题：官方 `decider.serve` 在 engine 加载并 seal 后 `GET /health` 立刻 200，但**第一次真实 forward 还要付**
权重 page-in + kernel JIT。本试用实测首条中文 choice `53089.933 ms`，之后 ~120ms（p95 124ms），
于是 health 200 之后马上到达的用户第一句会超时。

做法：`warmup/serve_warmup.py` 只**包一层官方 lifespan**（`decider.serve` 本体一字不改）：

1. 官方 lifespan 先跑完（engine 加载、seal、batcher 起来）；
2. 在 `yield`（也就是 uvicorn 对外可连）**之前**，走官方 `decider.serve.systemone` 路由函数本身
   跑 2 条与 Recordian 一致的中文 role choice，其中一条是 ~490 字的长 state；请求经过的就是真实请求
   用的同一个 CPU 线程池、队列、batcher 和 GPU 线程；
3. 两条都必须是**协议与数值合法**的 choice 分布（answer type 是 choice、labels 与 criteria 一致、
   每个概率是 [0,1] 内的有限数、概率和为 1±0.02、`choice` 属于 labels 且与最大概率一致，允许并列）
   才 `yield`；**不看置信度，也不看选得对不对**：均匀分布和高置信分布都合法，固定样例的标签正确性
   不是服务可用门槛。任何一条不合法就写 `logs/warmup-report.json` 并抛错 → uvicorn 打印
   `Application startup failed. Exiting.` 且**从不 bind 42071**，容器退出码非 0
   （uvicorn 自身启动失败时透传它的退出码，本机实测为 3；uvicorn 以 0 退出但报告 failed 时
   `serve-inner.sh` 用 7）。

这保证了就绪前已完成指定样例的真实推理。其他长度或不同并发负载仍需单独测量，不能由两条预热样例保证所有请求的延迟。未改：官方评分、temperature（1.935）、权重、
layout、未知 shape 的 eager 策略。`DECIDER_WARMUP=0` 保持不变（不捕获 CUDA graph），
不启用 `torch.compile`、不启用 FP8；wrapper 还会在启动时**断言**这些策略没漂移，漂移就拒绝启动。

kernel/JIT 缓存落在数据盘，重启复用：`TRITON_CACHE_DIR=/work/cache/triton`、
`XDG_CACHE_HOME=/work/cache/xdg`（comgr LLVM code objects）、
`MIOPEN_USER_DB_PATH=/work/cache/miopen/userdb`、`MIOPEN_CUSTOM_CACHE_DIR=/work/cache/miopen/kernels`。
**`serve-inner.sh` 不设置、也不重新赋值 `HOME`**：每个缓存都由它自己的变量显式钉在 `/work/cache`，
所以不需要也不应该改 `HOME`（容器默认 `HOME=/var/lib/gpustack/qwen-tts-home`，与 warmup 之前的官方入口
一致）。不设这些缓存变量时缓存会写进容器可写层，`docker rm` 后即丢失。

## 启动校验：按实际权重文件与源码 pin

`logs/serve-remote.sh` 在起容器前有四道闸（任一不过就拒绝，不覆盖现有实例）：

1. `MemAvailable ≥ 38GiB`（20GiB 邻居余量 + 自己 18GiB 上限）→ `exit 3`
2. 自己 `/health` 已 200 → `exit 6`（不覆盖健康实例）
3. 42071 被占 → `exit 4`
4. `warmup/weights-pin-check.py` → `exit 5`；以及源码 HEAD == pin → `exit 8`；warmup 文件缺失 → `exit 9`

第 4 条不再只看历史 `WEIGHTS_OK` 字符串，也不做分块抽样或 inode/mtime 信任：**每次启动前都对
固定权重文件做完整校验**——size 必须精确等于 8411558400，且整文件 sha256 必须等于 pin
`69e68954…1d936`（8.4GB 全量读取，几十秒，发生在 `docker run` 之前，不在任何请求路径上）。
没有 attestation 缓存，也没有“快速信任”路径：任何一次启动看到的都是当前磁盘上文件的完整摘要。
`download.log` 的 `WEIGHTS_OK` 只作为历史证据保留。手动单跑（可选）：

```bash
python3 /media/v/Data/recordian-decider-trial/warmup/weights-pin-check.py   # WEIGHTS_PIN OK mode=full-sha256
```

## 在现有 111 环境上传与启动

以下命令从仓库根目录运行，前提是 111 已有上文的基础镜像、源码、venv 和已下载权重。把 `<user>` 换成服务器登录用户。非交互运行前需配置可用的 SSH 认证。

```bash
# 0) 一次性：目标目录
ssh -o BatchMode=yes <user>@192.168.5.111 'mkdir -p /media/v/Data/recordian-decider-trial/{warmup,logs/rollback,cache}'

# 1) 从仓库根目录上传
scp -o BatchMode=yes server/decider_trial/scripts/serve_warmup.py \
    server/decider_trial/scripts/serve_warmup_selftest.py \
    server/decider_trial/scripts/weights-pin-check.py \
    <user>@192.168.5.111:/media/v/Data/recordian-decider-trial/warmup/
scp -o BatchMode=yes server/decider_trial/scripts/serve-inner.sh \
    server/decider_trial/scripts/serve-remote.sh \
    server/decider_trial/scripts/readiness-poll-remote.sh \
    server/decider_trial/scripts/acceptance-remote.sh \
    server/decider_trial/scripts/warmup-selftest-remote.sh \
    <user>@192.168.5.111:/media/v/Data/recordian-decider-trial/logs/
scp -o BatchMode=yes server/decider_trial/scripts/rollback-remote.sh \
    <user>@192.168.5.111:/media/v/Data/recordian-decider-trial/logs/rollback/

# 2) 启动（内部先过四道闸——含整文件 size+sha256 校验——再 docker run）
ssh -o BatchMode=yes <user>@192.168.5.111 'bash /media/v/Data/recordian-decider-trial/logs/serve-remote.sh'

# 3) 等就绪（每次最多 28s，可重复调用；不是后台 monitor）
ssh -o BatchMode=yes <user>@192.168.5.111 'bash /media/v/Data/recordian-decider-trial/logs/readiness-poll-remote.sh $(date -u +%s) 28'

# 4) 验收：2 条新的中文 role choice（各 <350ms）+ 分布合法性 + 容器/SemIf 状态
ssh -o BatchMode=yes <user>@192.168.5.111 'bash /media/v/Data/recordian-decider-trial/logs/acceptance-remote.sh'

# 5) 停止 / 回滚
ssh -o BatchMode=yes <user>@192.168.5.111 'docker stop recordian-decider-trial'
ssh -o BatchMode=yes <user>@192.168.5.111 'bash /media/v/Data/recordian-decider-trial/logs/rollback/rollback-remote.sh'
ssh -o BatchMode=yes <user>@192.168.5.111 'bash /media/v/Data/recordian-decider-trial/logs/rollback/rollback-remote.sh --restore-official'  # 回到无 warmup 的官方入口
```

本地测试与检查（不需要 GPU、不需要 111）：

```bash
python3 server/decider_trial/scripts/test_serve_warmup_local.py   # 17 个场景：合法（成功/均匀/高置信/轻微 flat），拒绝（坏 labels、坏 mass、NaN、inf、负值、>1、非数值、非 choice、choice 不在 labels、choice 非 argmax、官方路由报错、策略漂移），每个场景独立进程
python3 server/decider_trial/scripts/weights-pin-check.py --help
```

## 环境（`serve-inner.sh`）

`DECIDER_DEVICE=cuda`（ROCm 上映射到 HIP）、`DECIDER_COMPILE=0`、`DECIDER_FP8=0`、`DECIDER_WARMUP=0`、
`DECIDER_SCHEMA_CACHE=0`、`DECIDER_TEMPERATURE=1.935`、`DECIDER_TOKENIZE_THREADS=4`、`DECIDER_MAX_BATCH=1`、
`DECIDER_MAX_STATE_TOKENS=2048`、`DECIDER_SHARED=0`、`OMP/MKL/OPENBLAS_NUM_THREADS=4`、离线 HF。
入口：`uvicorn serve_warmup:app --app-dir /work/warmup --host 0.0.0.0 --port 42071 --workers 1`。

## 回滚

`logs/rollback/rollback-remote.sh`（只动本试用）：

1. `bash rollback-remote.sh`：停并删除 `recordian-decider-trial`、`-download`、`-warmupfailtest` 容器；数据保留。
2. `bash rollback-remote.sh --purge`：只打印（不执行）删除两个目录的命令。
3. `bash rollback-remote.sh --restore-official`：用 `logs/rollback/serve-inner.official-20260924.sh`
   （warmup 之前的官方入口备份）以同样的 docker run 参数重启，回到「health 先 200、首句慢」的行为。
4. 旧容器配置证据在 `logs/rollback/container-<id>-<utc>.json`（`docker inspect` 全文），
   `logs/rollback/start-args.log` 记录每次启动用的 docker run 参数。

不要动：镜像 `local/qwen-retrieval-gpustack:rocm`、SemIf（42032）、Winnow 目录与 42070、
主机 ROCm/驱动、生产 `.env`。只回滚本目录：`server/decider_trial/**` 与 `reports/decider-*`。
本试用没有共享 venv、没有装主机包、没有替换基础镜像的 torch，所以回滚不影响其他工作者。

## ROCm 支持状态（非官方）

decider 官方只声明 CUDA / MPS / CPU，**ROCm 不在官方支持列表内**：这里用 PyTorch ROCm 构建把
`DECIDER_DEVICE=cuda` 当 HIP 用。已验证 `torch.cuda.is_available()`、`gfx1151` 上 BF16 matmul 有限、
真实请求数值与预期一致；但这是试用结论，不是上游承诺。已知差异：镜像内没有 `causal_conv1d`，
`transformers` 会回退到 reference PyTorch 实现（更慢但数值正确）；eager（无 CUDA graph）路径
在 ROCm 上不可避免，因为 `DECIDER_WARMUP=0` 是本次明确选择。

## 试用状态

见 `reports/decider-warmup-final.json` / `.md`（本次 warmup 就绪改造的 hash、启动耗时、readiness 时间线、
两条新句与延迟、原始返回）与 `reports/decider-deployment.json`（首次部署与首请求证据）。
